"""In-memory fakes so assertions and reports can be tested without a lab."""

from __future__ import annotations

import heapq
import ipaddress
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from netcheck.backends.base import Backend
from netcheck.devices.assembly import CapabilityFactory, DeviceSpec, assemble
from netcheck.devices.device import Device
from netcheck.model.capabilities import (
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
from netcheck.model.expected import Expected
from netcheck.model.intent import Intent, Link
from netcheck.run.lab import Lab
from netcheck.run.timing import Threshold, Timing


class FakeOspf(Ospf):
    def __init__(self) -> None:
        self.nbrs: list[OspfNeighbor] = []
        self.rib: dict[str, Route] = {}
        self.lsdb: set[str] = set()
        self.interfaces: dict[str, OspfInterface] = {}  # what the router reports
        self.area_overrides: dict[str, int | None] = {}
        # set by make_healthy: neighbors, routes and LSDB then come from the computed
        # OSPF domain and follow link changes, instead of from nbrs / rib / lsdb
        self.network: FakeOspfNetwork | None = None
        self.name = ""

    def neighbors(self) -> list[OspfNeighbor]:
        return self.network.neighbors(self.name) if self.network else self.nbrs

    def routes(self) -> dict[str, Route]:
        return self.network.routes(self.name) if self.network else self.rib

    def lsdb_prefixes(self) -> set[str]:
        return set(self.network.routes(self.name)) if self.network else self.lsdb

    def interface_settings(self) -> dict[str, OspfInterface]:
        """`interfaces`, with any set_interface_area overrides applied."""
        settings = dict(self.interfaces)
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
    """Ping succeeds when FakeWorld says the host reaches the address. An on-link route to
    the destination (the isolation test adds one) means layer 2 only. A ping resolved over
    layer 2 leaves a neighbor entry, REACHABLE or FAILED, as the kernel does;
    `arp_silent` stands for a host whose probes never reach the wire (no entry)."""

    def __init__(self, name: str, world: FakeWorld) -> None:
        self.name = name
        self.world = world
        self.routes: list[str] = []
        self.neigh: dict[str, str] = {}  # address -> state
        self.arp_silent = False
        self.lost_per_stream: list[int] = []  # probes lost in each ping stream, in order

    def ping(
        self,
        dst: str,
        *,
        count: int = 3,
        interval: float = 0.2,
        deadline: float = 4.0,
        src_port: str = "p1",
    ) -> PingResult:
        on_link = f"{dst}/32" in self.routes
        reached = self.world.reaches(self.name, dst, on_link=on_link)
        if (on_link or self.world.same_subnet(self.name, dst)) and not self.arp_silent:
            self.neigh[dst] = "REACHABLE" if reached else "FAILED"
        return PingResult(self.name, dst, count, count if reached else 0)

    def add_host_route(self, prefix: str, port: str = "p1") -> None:
        self.routes.append(prefix)

    def del_host_route(self, prefix: str, port: str = "p1") -> None:
        self.routes.remove(prefix)

    def flush_neighbors(self, port: str = "p1") -> None:
        self.neigh.clear()

    def neighbors(self) -> list[dict[str, Any]]:
        return [
            {"dst": ip, "state": [state]}
            | ({"lladdr": f"aa:c1:ab:00:00:{ip.split('.')[-1]}"} if state == "REACHABLE" else {})
            for ip, state in sorted(self.neigh.items())
        ]

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
        """1000 probes, 10 lost; or the next of `lost_per_stream`, one per measurement."""
        lost = self.lost_per_stream.pop(0) if self.lost_per_stream else 10
        return PingResult(self.name, "stream", 1000, 1000 - lost)


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
    """Hosts, switches and VLANs, for the fake ping.

    Two hosts share a broadcast domain when the access VLAN of one is on the other's port
    and every trunk between their switches carries it, read from the fake bridges' current
    state (so an injected wrong-VLAN fault joins them). A ping goes over layer 2 when it
    has an on-link route or the address is in the sender's subnet; otherwise it goes to
    the gateway, and gets replies when both hosts have one (the fake routers route between
    every LAN) or when `leaks` joins the pair (a router that should not). A VLAN gateway's
    address answers a host whose port carries that VLAN.
    """

    def __init__(self, intent: Intent) -> None:
        self.intent = intent
        self.expected = Expected(intent)
        self.bridges: dict[str, FakeBridge] = {}  # by switch, filled by FakeFactory
        self.leaks: set[frozenset[str]] = set()  # host pairs a router joins by mistake

    def host_at(self, ip: str) -> str | None:
        return next(
            (n.name for n in self.intent.nodes.values() if n.ip and n.ip.split("/")[0] == ip),
            None,
        )

    def same_subnet(self, src: str, dst_ip: str) -> bool:
        net = ipaddress.ip_interface(self.intent.nodes[src].ip or "").network
        return ipaddress.ip_address(dst_ip) in net

    def _gateway_vlan(self, ip: str) -> int | None:
        return next(
            (
                vid
                for vid, v in self.intent.vlans.items()
                if v.gateway_ip and v.gateway_ip.split("/")[0] == ip
            ),
            None,
        )

    def _vlans(self, switch: str, port: str) -> set[int]:
        pv = self.bridges[switch].ports.get(port)
        return set(pv.vlans) if pv else set()

    def _domain(self, switch: str, vid: int) -> set[str]:
        """Switches joined to `switch` by trunks that carry `vid` on both ends."""
        seen, todo = {switch}, [switch]
        while todo:
            here = todo.pop()
            for lk in self.intent.links_of(here):
                far = lk.peer(here)
                if (
                    far.node in self.bridges
                    and far.node not in seen
                    and vid in self._vlans(here, lk.endpoint(here).port)
                    and vid in self._vlans(far.node, far.port)
                ):
                    seen.add(far.node)
                    todo.append(far.node)
        return seen

    def same_l2(self, a: str, b: str) -> bool:
        at_a, at_b = self.expected.attachment(a), self.expected.attachment(b)
        if at_a is None or at_b is None:
            return False
        return any(
            vid in self._vlans(at_b.switch, at_b.port)
            and at_b.switch in self._domain(at_a.switch, vid)
            for vid in self._vlans(at_a.switch, at_a.port)
        )

    def reaches(self, src: str, dst_ip: str, *, on_link: bool) -> bool:
        dst = self.host_at(dst_ip)
        if dst is None:  # a VLAN gateway's address
            vid, at = self._gateway_vlan(dst_ip), self.expected.attachment(src)
            return vid is not None and at is not None and vid in self._vlans(at.switch, at.port)
        if on_link or self.same_subnet(src, dst_ip):
            return self.same_l2(src, dst)
        nodes = self.intent.nodes
        both_routed = bool(nodes[src].gateway and nodes[dst].gateway)
        return both_routed or frozenset((src, dst)) in self.leaks


DEFAULT_OSPF_COST = 10  # a link the intent gives no ospf_cost (FRR would derive one)


class FakeOspfNetwork:
    """A working OSPF domain computed from the intent, for make_healthy.

    An adjacency is Full on every routed router-to-router link whose ports are up on both
    ends. Each router advertises the networks of its intended OSPF interfaces that are up.
    Routes are a shortest-path calculation over the intent's OSPF costs, keeping every
    equal-cost first hop, redone on each read, so taking a link down reroutes. It does not
    use Expected's route or next-hop queries, so it can disagree with them.
    """

    def __init__(self, lab: Lab, ospf: dict[str, FakeOspf]) -> None:
        self.intent = lab.intent
        self.routers = lab.expected.routers()
        self.links = {r: lab[r].links for r in self.routers}
        self.ospf = ospf  # live settings: an interface taken out of OSPF stops advertising

    def _up(self, router: str, port: str) -> bool:
        return self.links[router].link_state(port.partition(".")[0]).admin_up

    def _link_up(self, lk: Link) -> bool:
        return all(ep.node not in self.links or self._up(ep.node, ep.port) for ep in (lk.a, lk.b))

    def _core(self, router: str) -> list[Link]:
        """Routed links from `router` to another router, up on both ends."""
        return [
            lk
            for lk in self.intent.links_of(router)
            if lk.routed and lk.peer(router).node in self.links and self._link_up(lk)
        ]

    def neighbors(self, router: str) -> list[OspfNeighbor]:
        return [
            OspfNeighbor(
                self.intent.nodes[lk.peer(router).node].router_id or "",
                "Full",
                lk.endpoint(router).port,
            )
            for lk in self._core(router)
        ]

    def _advertised(self, router: str) -> dict[str, int]:
        """{prefix: interface cost} for the router's OSPF interfaces that are up and in
        an area."""
        out: dict[str, int] = {}
        for name, iface in self.ospf[router].interface_settings().items():
            if iface.area is None:
                continue
            port = name.partition(".")[0]
            lk = self.intent.link_at(router, port)
            if not self._link_up(lk):
                continue
            for address in iface.addresses:
                net = str(ipaddress.ip_interface(address).network)
                out[net] = lk.ospf_cost or DEFAULT_OSPF_COST
        return out

    def _spf(self, root: str) -> tuple[dict[str, int], dict[str, set[str]]]:
        """Distance to every reachable router, and the first-hop addresses of all
        shortest paths to it (Dijkstra with equal-cost paths merged)."""
        dist, first = {root: 0}, {root: set[str]()}
        queue, done = [(0, root)], set()
        while queue:
            d, here = heapq.heappop(queue)
            if here in done:
                continue
            done.add(here)
            for lk in self._core(here):
                far = lk.peer(here)
                cost = d + (lk.ospf_cost or DEFAULT_OSPF_COST)
                hops = {(far.ip or "").split("/")[0]} if here == root else first[here]
                if far.node not in dist or cost < dist[far.node]:
                    dist[far.node], first[far.node] = cost, set(hops)
                    heapq.heappush(queue, (cost, far.node))
                elif cost == dist[far.node]:
                    first[far.node] |= hops
        return dist, first

    def routes(self, router: str) -> dict[str, Route]:
        dist, first = self._spf(router)
        mine = self._advertised(router)
        best: dict[str, tuple[int, set[str]]] = {}
        for origin in self.routers:
            if origin == router or origin not in dist:
                continue
            for prefix, iface_cost in self._advertised(origin).items():
                if prefix in mine:
                    continue
                cost = dist[origin] + iface_cost
                if prefix not in best or cost < best[prefix][0]:
                    best[prefix] = (cost, set(first[origin]))
                elif cost == best[prefix][0]:
                    best[prefix][1].update(first[origin])
        return {
            p: Route(p, "ospf", tuple(sorted(hops)), True) for p, (_, hops) in sorted(best.items())
        }


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
    """The fakes, handed out through the real assembly. Routers get a FakeOspf, each
    switch a FakeBridge, hosts a FakeShell; each one is kept in `parts`."""

    def __init__(self, world: FakeWorld, parts: dict[str, Any]) -> None:
        self.world, self.parts = world, parts

    def links(self, spec: DeviceSpec) -> Links:
        return FakeLinks()

    def ospf(self, spec: DeviceSpec) -> Ospf:
        o = FakeOspf()
        self.parts[f"ospf_{spec.name}"] = o
        return o

    def bridge(self, spec: DeviceSpec) -> Bridge:
        """One bridge per switch, starting with its intended VLANs. parts["bridge"] is the
        first switch's, for tests written against campus-mini's single sw1."""
        b = FakeBridge(self.world.expected.vlan_membership(spec.name))
        self.world.bridges[spec.name] = b
        self.parts[f"bridge_{spec.name}"] = b
        self.parts.setdefault("bridge", b)
        return b

    def shell(self, spec: DeviceSpec) -> Shell | None:
        if spec.role != "host":
            return None
        self.parts[f"shell_{spec.name}"] = FakeShell(spec.name, self.world)
        return self.parts[f"shell_{spec.name}"]

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
    world = FakeWorld(intent)
    parts: dict[str, Any] = {}
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


def make_healthy(lab: Lab, parts: dict[str, Any]) -> None:
    """Fill the fakes with the state of a working lab built from `lab.intent`: every router
    reports its intended OSPF interfaces, and its adjacencies, routes and LSDB come from a
    FakeOspfNetwork, so they follow link changes. On campus-mini, r1 routes h4's LAN via
    10.0.12.1 while p2 is up and via 10.0.12.3 when it is down."""
    network = FakeOspfNetwork(lab, {r: parts[f"ospf_{r}"] for r in lab.expected.routers()})
    for router in lab.expected.routers():
        ospf = parts[f"ospf_{router}"]
        ospf.interfaces = lab.expected.ospf_interfaces(router)
        ospf.network, ospf.name = network, router
