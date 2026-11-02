"""In-memory fakes so assertions and reports can be tested without a lab."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from netcheck.backends.base import Backend
from netcheck.capabilities import (
    Bridge,
    CaptureHandle,
    Links,
    Ospf,
    OspfNeighbor,
    PingResult,
    PortVlans,
    Route,
    Shell,
)
from netcheck.device import Device
from netcheck.devicelog import DeviceLog
from netcheck.intent import Intent, expected_state
from netcheck.lab import Lab
from netcheck.timing import Threshold, Timing


class FakeOspf(Ospf):
    def __init__(self) -> None:
        self.nbrs: list[OspfNeighbor] = []
        self.rib: dict[str, Route] = {}
        self.lsdb: set[str] = set()
        self.config = ""

    def neighbors(self) -> list[OspfNeighbor]:
        return self.nbrs

    def routes(self) -> dict[str, Route]:
        return self.rib

    def lsdb_prefixes(self) -> set[str]:
        return self.lsdb

    def running_config(self) -> str:
        return self.config


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

    def link_state(self, port: str) -> dict[str, str]:
        up = self.admin.get(port, True)
        return {"port": port, "admin": "up" if up else "down", "operstate": "UP" if up else "DOWN"}


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


def port_name(handle: CaptureHandle) -> str:
    return handle.port


class FakeWorld:
    def __init__(self, intent: Intent, bridge: FakeBridge) -> None:
        self.intent = intent
        self.bridge = bridge

    def same_vlan(self, src: str, dst_ip: str) -> bool:
        from netcheck.assertions import host_attachment

        dst = next(
            n for n in self.intent.nodes.values() if n.ip and n.ip.split("/")[0] == dst_ip
        ).name
        _, pa, _ = host_attachment(self.intent, src)
        _, pb, _ = host_attachment(self.intent, dst)
        va = set(self.bridge.ports[pa].vlans)
        vb = set(self.bridge.ports[pb].vlans)
        return bool(va & vb)


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, intent: Intent, devices: dict[str, Device]) -> None:
        super().__init__(intent, {}, Path("."))
        self._devices = devices
        self.restored: list[str] = []

    def deploy(self, fresh: bool = False) -> bool:
        return False

    def destroy(self) -> None:
        pass

    def is_running(self) -> bool:
        return True

    def devices(self) -> dict[str, Device]:
        return self._devices

    def snapshot(self, label: str) -> str:
        return f"{label}-fake"

    def restore(self, snapshot_id: str) -> None:
        self.restored.append(snapshot_id)

    def environment(self) -> dict[str, str]:
        return {"topology": self.intent.name, "backend": "fake", "intent": self.intent.digest}


def fast_timing() -> Timing:
    """Tiny timeouts so failing waits end immediately in unit tests."""
    th = {
        name: Threshold(name, timeout=0.05, poll=0.01, sla=5.0)
        for name in (
            "route_installed",
            "ospf_neighbor_full",
            "interface_up",
            "reconverge_local_link",
        )
    }
    th["isolation_window"] = Threshold("isolation_window", 0, 0, window=1.0)
    return Timing(profile="unit", thresholds=th)


def build_fake_lab(intent: Intent, tmp: Path) -> tuple[Lab, dict[str, Any]]:
    expected = expected_state(intent)
    sw_ports = {
        p: PortVlans(v["mode"], tuple(v["vlans"]))
        for p, v in expected["vlan_membership"]["sw1"].items()
    }
    bridge = FakeBridge(sw_ports)
    world = FakeWorld(intent, bridge)
    devices: dict[str, Device] = {}
    parts: dict[str, Any] = {"bridge": bridge}
    for name, n in intent.nodes.items():
        caps: dict[type, object] = {Links: FakeLinks()}
        if n.role == "router":
            o = FakeOspf()
            caps[Ospf] = o
            parts[f"ospf_{name}"] = o
        if n.role == "switch":
            caps[Bridge] = bridge
        if n.role == "host":
            caps[Shell] = FakeShell(name, world)
        ports = {
            ep.port: f"eth{ep.port[1:]}"
            for lk in intent.links_of(name)
            for ep in (lk.a, lk.b)
            if ep.node == name
        }
        devices[name] = Device(name, n.role, "fake", DeviceLog(name), caps, ports)
    lab = Lab(
        intent=intent,
        expected=expected,
        backend=FakeBackend(intent, devices),
        devices=devices,
        timing=fast_timing(),
        artifacts_dir=tmp,
        test_artifacts=tmp,
    )
    return lab, parts
