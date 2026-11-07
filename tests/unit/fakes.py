"""In-memory fakes so assertions and reports can be tested without a lab."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from netcheck.assembly import CapabilityFactory, DeviceSpec, assemble
from netcheck.backends.base import Backend
from netcheck.backends.local import parse_frr_interfaces
from netcheck.capabilities import (
    Bridge,
    CaptureHandle,
    Links,
    LinkState,
    Ospf,
    OspfInterface,
    OspfNeighbor,
    PingResult,
    PortVlans,
    Route,
    Shell,
    Snapshot,
)
from netcheck.device import Device
from netcheck.expected import Expected
from netcheck.intent import Intent
from netcheck.lab import Lab
from netcheck.ports import PortMap
from netcheck.timing import Threshold, Timing


class FakeOspf(Ospf):
    def __init__(self) -> None:
        self.nbrs: list[OspfNeighbor] = []
        self.rib: dict[str, Route] = {}
        self.lsdb: set[str] = set()
        self.config = ""
        self.area_overrides: dict[str, int | None] = {}

    def neighbors(self) -> list[OspfNeighbor]:
        return self.nbrs

    def routes(self) -> dict[str, Route]:
        return self.rib

    def lsdb_prefixes(self) -> set[str]:
        return self.lsdb

    def interface_settings(self) -> dict[str, OspfInterface]:
        """Parsed from `config` (FRR format, ethN = pN) with the backend's own parser,
        then any set_interface_area overrides applied."""
        settings = parse_frr_interfaces(
            self.config, PortMap({f"p{i}": f"eth{i}" for i in range(1, 10)})
        )
        for port, area in self.area_overrides.items():
            current = settings.get(port, OspfInterface(port))
            settings[port] = replace(current, area=None if area is None else str(area))
        return settings

    def set_interface_area(self, port: str, area: int | None) -> None:
        self.area_overrides[port] = area


class FakeBridge(Bridge):
    def __init__(self, ports: dict[str, PortVlans]) -> None:
        self.ports = ports

    def vlan_membership(self) -> dict[str, PortVlans]:
        return dict(self.ports)

    def set_access_vlan(self, port: str, vlan: int) -> None:
        self.ports[port] = PortVlans("access", (vlan,))


class FakeLinks(Links):
    def __init__(self) -> None:
        self.admin: dict[str, bool] = {}

    def set_link(self, port: str, up: bool) -> None:
        self.admin[port] = up

    def link_state(self, port: str) -> LinkState:
        up = self.admin.get(port, True)
        return LinkState(port, f"eth{port[1:]}", up, up)


class FakeShell(Shell):
    """Ping succeeds when the two hosts share a VLAN on the fake switch."""

    def __init__(self, name: str, world: FakeWorld) -> None:
        self.name = name
        self.world = world
        self.routes: list[str] = []

    def ping(
        self,
        dst: str,
        *,
        count: int = 3,
        interval: float = 0.2,
        deadline: float = 4.0,
        src_port: str = "p1",
    ) -> PingResult:
        same = self.world.same_vlan(self.name, dst)
        return PingResult(self.name, dst, count, count if same else 0)

    def add_host_route(self, prefix: str, port: str = "p1") -> None:
        self.routes.append(prefix)

    def del_host_route(self, prefix: str, port: str = "p1") -> None:
        self.routes.remove(prefix)

    def flush_neighbors(self, port: str = "p1") -> None:
        pass

    def neighbors(self) -> list[dict[str, str]]:
        return [{"dst": "10.0.20.13", "lladdr": "aa:c1:ab:00:00:03", "state": "REACHABLE"}]

    def start_capture(self, port: str, bpf: str = "") -> CaptureHandle:
        return CaptureHandle(self.name, port, "/tmp/x.pcap", "1")

    def stop_capture(self, handle: CaptureHandle, dest_dir: str) -> CaptureHandle:
        if handle.stopped:
            return handle
        handle.stopped = True
        handle.summary = [
            "IP 10.0.10.11 > 10.0.20.13: ICMP echo request",
            "IP 10.0.20.13 > 10.0.10.11: ICMP echo reply",
        ]
        handle.local_path = str(Path(dest_dir) / f"{self.name}-{port_name(handle)}.pcap")
        return handle

    def start_stream(self, dst: str, interval: float, deadline: float) -> str:
        return "1:/tmp/s"

    def stop_stream(self, token: str) -> PingResult:
        return PingResult(self.name, "stream", 300, 290)


class FakeSnapshot(Snapshot):
    """Takes and restores the fakes' state: link admin, bridge VLANs, OSPF area overrides.
    `fail_with` makes restore_to raise, as a device that cannot be restored would."""

    def __init__(
        self, links: FakeLinks, bridge: FakeBridge | None = None, ospf: FakeOspf | None = None
    ) -> None:
        self.links, self.bridge, self.ospf = links, bridge, ospf
        self.fail_with: Exception | None = None
        self.restores = 0

    def take(self) -> Any:
        return {
            "links": dict(self.links.admin),
            "bridge": {p: [v.mode, list(v.vlans)] for p, v in self.bridge.ports.items()}
            if self.bridge
            else None,
            "ospf_areas": dict(self.ospf.area_overrides) if self.ospf else None,
        }

    def restore_to(self, state: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.restores += 1
        self.links.admin = dict(state["links"])
        if self.bridge is not None:
            self.bridge.ports = {p: PortVlans(m, tuple(v)) for p, (m, v) in state["bridge"].items()}
        if self.ospf is not None:
            self.ospf.area_overrides = dict(state["ospf_areas"])


def port_name(handle: CaptureHandle) -> str:
    return handle.port


class FakeWorld:
    def __init__(self, intent: Intent, bridge: FakeBridge) -> None:
        self.intent = intent
        self.bridge = bridge

    def same_vlan(self, src: str, dst_ip: str) -> bool:
        dst = next(
            n for n in self.intent.nodes.values() if n.ip and n.ip.split("/")[0] == dst_ip
        ).name
        expected = Expected(self.intent)
        at_a, at_b = expected.attachment(src), expected.attachment(dst)
        if at_a is None or at_b is None:
            return True  # one end is behind a router: the fake world routes it
        va = set(self.bridge.ports[at_a.port].vlans)
        vb = set(self.bridge.ports[at_b.port].vlans)
        return bool(va & vb)


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, intent: Intent, devices: dict[str, Device], root: Path) -> None:
        super().__init__(intent, {}, root)
        self._devices = devices
        self.running = True  # a lab is up; set False for "nothing deployed yet"
        self.deploys = 0

    def deploy(self) -> None:
        self.deploys += 1
        self.running = True

    def destroy(self) -> None:
        pass

    def is_running(self) -> bool:
        return self.running

    def devices(self) -> dict[str, Device]:
        return self._devices

    def environment(self) -> dict[str, str]:
        return {"topology": self.intent.name, "backend": "fake", "intent": self.intent.digest}


def fast_timing() -> Timing:
    """Tiny timeouts so failing waits end immediately in unit tests."""
    th = {
        name: Threshold(name, timeout=0.05, poll=0.01, sla=5.0)
        for name in (
            "route_installed",
            "reachability",
            "ospf_neighbor_full",
            "interface_up",
            "reconverge_local_link",
        )
    }
    th["isolation_window"] = Threshold("isolation_window", 0, 0, window=1.0)
    return Timing(profile="unit", thresholds=th)


class FakeFactory(CapabilityFactory):
    """The fakes, handed out through the real assembly. Routers get a FakeOspf, the
    switch the shared FakeBridge, hosts a FakeShell; each one is kept in `parts`."""

    def __init__(self, world: FakeWorld, parts: dict[str, Any]) -> None:
        self.world, self.parts = world, parts

    def links(self, spec: DeviceSpec) -> Links:
        return FakeLinks()

    def ospf(self, spec: DeviceSpec) -> Ospf:
        o = FakeOspf()
        self.parts[f"ospf_{spec.name}"] = o
        return o

    def bridge(self, spec: DeviceSpec) -> Bridge:
        return self.world.bridge

    def shell(self, spec: DeviceSpec) -> Shell | None:
        return FakeShell(spec.name, self.world) if spec.role == "host" else None

    def snapshot(self, spec: DeviceSpec, built: dict[type, object]) -> Snapshot:
        snap = FakeSnapshot(
            cast(FakeLinks, built[Links]),
            cast(FakeBridge, built.get(Bridge)),
            cast(FakeOspf, built.get(Ospf)),
        )
        self.parts[f"snapshot_{spec.name}"] = snap
        return snap


def build_fake_lab(intent: Intent, tmp: Path) -> tuple[Lab, dict[str, Any]]:
    expected = Expected(intent)
    bridge = FakeBridge(expected.vlan_membership("sw1"))
    world = FakeWorld(intent, bridge)
    parts: dict[str, Any] = {"bridge": bridge}
    devices = assemble(intent, "fake", "eth", FakeFactory(world, parts))
    lab = Lab(
        intent=intent,
        expected=expected,
        backend=FakeBackend(intent, devices, tmp),
        devices=devices,
        timing=fast_timing(),
        artifacts_dir=tmp,
    )
    return lab, parts


# ---- a healthy lab, for running the real tests/lab files on fakes -----------------

_R1_CONFIG = (
    "interface eth1.10\n ip address 10.0.10.1/24\n ip ospf area 0\n ip ospf passive\nexit\n!\n"
    "interface eth2\n ip address 10.0.12.0/31\n ip ospf area 0\n ip ospf dead-interval 4\n"
    " ip ospf hello-interval 1\nexit\n!\n"
    "interface eth3\n ip address 10.0.12.2/31\n ip ospf area 0\n ip ospf dead-interval 4\n"
    " ip ospf hello-interval 1\nexit\n!\n"
)
_R2_CONFIG = (
    "interface eth1\n ip address 10.0.12.1/31\n ip ospf area 0\n ip ospf dead-interval 4\n"
    " ip ospf hello-interval 1\nexit\n!\n"
    "interface eth2\n ip address 10.0.12.3/31\n ip ospf area 0\n ip ospf dead-interval 4\n"
    " ip ospf hello-interval 1\nexit\n!\n"
    "interface eth3\n ip address 10.0.30.1/24\n ip ospf area 0\n ip ospf passive\nexit\n!\n"
)


def make_healthy(lab: Lab, parts: dict[str, Any]) -> None:
    """Fill the fakes with the state of a working campus-mini lab. r1's route to h4's LAN
    follows r1's primary link: via 10.0.12.1 while p2 is up, via 10.0.12.3 when it is down."""
    r1, r2 = parts["ospf_r1"], parts["ospf_r2"]
    r1.config, r2.config = _R1_CONFIG, _R2_CONFIG
    r1.nbrs = [OspfNeighbor("2.2.2.2", "Full", "p2"), OspfNeighbor("2.2.2.2", "Full", "p3")]
    r2.nbrs = [OspfNeighbor("1.1.1.1", "Full", "p1"), OspfNeighbor("1.1.1.1", "Full", "p2")]
    r1_links = lab["r1"].links

    def r1_routes() -> dict[str, Route]:
        primary_up = r1_links.link_state("p2").admin_up
        hop = "10.0.12.1" if primary_up else "10.0.12.3"
        return {"10.0.30.0/24": Route("10.0.30.0/24", "ospf", (hop,), True)}

    r1.routes = r1_routes  # type: ignore[method-assign]
    r2.rib = {"10.0.10.0/24": Route("10.0.10.0/24", "ospf", ("10.0.12.0",), True)}
    r1.lsdb = {"10.0.30.0/24"}
    r2.lsdb = {"10.0.10.0/24"}
