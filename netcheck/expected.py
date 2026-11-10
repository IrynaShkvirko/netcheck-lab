"""Expected state: what the live network must look like if it follows the intent (ADR-0001).

`Expected(intent)` answers the questions tests and assertions ask, in the same normalized
types the capabilities return, so expected and live state compare directly. It is also
the third generated output: `to_json()` is `build/<topology>/expected_state.json`.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Any

from netcheck.capabilities import OspfInterface, PortVlans
from netcheck.intent import Intent, Link


@dataclass(frozen=True)
class Attachment:
    """The switch port a host is plugged into, and its VLAN."""

    switch: str
    port: str
    vlan: int | None


@dataclass(frozen=True)
class Adjacency:
    """One OSPF adjacency, seen from `router`: two links between the same routers are two."""

    router: str
    neighbor: str
    router_id: str  # the neighbor's
    local_port: str
    link: str  # link label, or "<port>-<port>"


@dataclass(frozen=True)
class NextHop:
    """The first hop of a cheapest OSPF path: the neighbor, the local port towards it,
    and the neighbor's address on that link (what the routing table shows)."""

    neighbor: str
    port: str
    address: str


@dataclass(frozen=True)
class FailoverPlan:
    """What the failover test needs, all derived from the intent."""

    router: str  # router whose primary link is taken down
    primary_port: str
    primary_hop: str  # next hop while the primary is up
    backup_hop: str  # next hop after the failure
    prefix: str  # LAN behind the far router
    src: str  # host sending traffic
    dst: str  # host on the far LAN


INFINITE = float("inf")


def _network(ip: str) -> str:
    return str(ipaddress.ip_interface(ip).network)


class Expected:
    def __init__(self, intent: Intent) -> None:
        self.intent = intent
        self._routers = sorted(r.name for r in intent.by_role("router"))
        self._originated = self._ospf_prefixes()

    # ---- switches -------------------------------------------------------------------
    def switches(self) -> list[str]:
        return sorted(s.name for s in self.intent.by_role("switch"))

    def vlan_membership(self, switch: str) -> dict[str, PortVlans]:
        """Mode and VLANs of every switch port the intent lists ({} for a non-switch)."""
        ports: dict[str, PortVlans] = {}
        if switch not in self.switches():
            return ports
        for lk in self.intent.links_of(switch):
            ep = lk.endpoint(switch)
            if lk.mode == "access" and lk.vlan is not None:
                ports[ep.port] = PortVlans("access", (lk.vlan,))
            elif lk.mode == "trunk":
                ports[ep.port] = PortVlans("trunk", tuple(sorted(lk.vlans)))
        return dict(sorted(ports.items()))

    # ---- routers and OSPF -------------------------------------------------------------
    def routers(self) -> list[str]:
        return list(self._routers)

    def routes(self, router: str) -> frozenset[str]:
        """Prefixes `router` must learn via OSPF: what the others originate, minus its own."""
        mine = self._originated.get(router, set())
        learned = set().union(*(p for r, p in self._originated.items() if r != router))
        return frozenset(learned - mine)

    def routes_by_router(self) -> dict[str, frozenset[str]]:
        return {r: self.routes(r) for r in self._routers}

    def originator(self, prefix: str, exclude: str = "") -> str:
        """The router that originates `prefix` into OSPF ('?' if none)."""
        owners = [r for r, ps in self._originated.items() if prefix in ps and r != exclude]
        return owners[0] if owners else "?"

    def ospf_next_hops(self, router: str, prefix: str) -> list[NextHop]:
        """The first hops `router` must use for `prefix` while every link is up.

        Each link from `router` that starts a cheapest path to a router originating the
        prefix gives one, so equal-cost paths give several (OSPF installs them all). A
        path costs the sum of its links' ospf_cost. The originating interface's cost is
        left out: only the network of a link between two routers has two originators, and
        both add that link's cost, so it never changes which path wins. Empty if `router`
        originates the prefix itself or no router reaches it.
        """
        if prefix in self._originated.get(router, set()):
            return []
        costs = self._path_costs()
        origins = [
            o
            for o, prefixes in self._originated.items()
            if o != router and prefix in prefixes and costs[router][o] != INFINITE
        ]
        if not origins:
            return []

        best = min(costs[router][o] for o in origins)
        hops = set()
        for origin in (o for o in origins if costs[router][o] == best):
            for lk in self._core_links():
                if router not in (lk.a.node, lk.b.node):
                    continue
                far = lk.peer(router)
                if (lk.ospf_cost or 0) + costs[far.node][origin] == costs[router][origin]:
                    address = (far.ip or "").split("/")[0]
                    hops.add(NextHop(far.node, lk.endpoint(router).port, address))
        return sorted(hops, key=lambda h: (h.neighbor, h.port))

    def _core_links(self) -> list[Link]:
        """Routed links between two routers: where OSPF paths run."""
        routers = set(self._routers)
        return [lk for lk in self.intent.links if lk.routed and {lk.a.node, lk.b.node} <= routers]

    def _path_costs(self) -> dict[str, dict[str, float]]:
        """Cheapest OSPF cost between every two routers (Floyd-Warshall over the core
        links' ospf_cost, which validation requires)."""
        cost = {a: {b: 0.0 if a == b else INFINITE for b in self._routers} for a in self._routers}
        for lk in self._core_links():
            a, b = lk.a.node, lk.b.node
            cost[a][b] = cost[b][a] = min(cost[a][b], float(lk.ospf_cost or 0))
        for via in self._routers:
            for a in self._routers:
                for b in self._routers:
                    if cost[a][via] + cost[via][b] < cost[a][b]:
                        cost[a][b] = cost[a][via] + cost[via][b]
        return cost

    def adjacencies(self) -> list[Adjacency]:
        """Every OSPF adjacency, from both ends, grouped by router."""
        out = []
        for r in self._routers:
            for lk in self.intent.links_of(r):
                peer = lk.peer(r)
                if lk.routed and peer.node in self._routers:
                    me = lk.endpoint(r)
                    out.append(
                        Adjacency(
                            router=r,
                            neighbor=peer.node,
                            router_id=self.intent.nodes[peer.node].router_id or "",
                            local_port=me.port,
                            link=lk.label or f"{me.port}-{peer.port}",
                        )
                    )
        return out

    def router_link_ports(self, router: str) -> list[str]:
        """Ports of `router` on routed links to other routers (where adjacencies form)."""
        return [a.local_port for a in self.adjacencies() if a.router == router]

    def links_between(self, a: str, b: str) -> list[Link]:
        return [lk for lk in self.intent.links if {lk.a.node, lk.b.node} == {a, b}]

    def ospf_interfaces(self, router: str) -> dict[str, OspfInterface]:
        """The OSPF interfaces `router` must have, as a healthy router reports them, by
        logical name: routed ports (p2) and VLAN gateway sub-interfaces (p1.10).
        Passive interfaces carry no timers or network type; cost only where the link
        sets one. In link order, which is also the order render writes them."""
        out: dict[str, OspfInterface] = {}
        if router not in self._routers:
            return out
        o = self.intent.ospf
        area = str(o.area)
        for lk in self.intent.links_of(router):
            ep = lk.endpoint(router)
            if lk.mode == "trunk":
                for vid in lk.vlans:
                    vlan = self.intent.vlans[vid]
                    if vlan.gateway_node == router and vlan.gateway_ip:
                        name = f"{ep.port}.{vid}"
                        out[name] = OspfInterface(name, (vlan.gateway_ip,), area, passive=True)
                continue
            if not ep.ip:
                continue
            if lk.ospf_passive:
                out[ep.port] = OspfInterface(ep.port, (ep.ip,), area, passive=True)
            else:
                out[ep.port] = OspfInterface(
                    ep.port,
                    (ep.ip,),
                    area,
                    hello_interval=o.hello_interval,
                    dead_interval=o.dead_interval,
                    cost=lk.ospf_cost,
                    network=o.network,
                )
        return out

    def advertising_interfaces(self, router: str, prefix: str) -> list[OspfInterface]:
        """The interfaces of `router` that put `prefix` into OSPF."""
        return [
            i
            for i in self.ospf_interfaces(router).values()
            if any(_network(a) == prefix for a in i.addresses)
        ]

    # ---- hosts ------------------------------------------------------------------------
    def attachment(self, host: str) -> Attachment | None:
        """Where a host is plugged into a switch; None if it hangs off a router."""
        for lk in self.intent.links_of(host):
            peer = lk.peer(host)
            if self.intent.nodes[peer.node].role == "switch":
                return Attachment(peer.node, peer.port, lk.vlan)
        return None

    def broadcast_domain(self, host: str) -> list[str]:
        """The other hosts that share `host`'s broadcast domain by the intent: on access
        ports of its VLAN, on its switch or on a switch joined to it by trunks that carry
        the VLAN. Empty for a host on a router or alone in its VLAN."""
        at = self.attachment(host)
        if at is None or at.vlan is None:
            return []
        switches, todo = {at.switch}, [at.switch]
        while todo:
            here = todo.pop()
            for lk in self.intent.links_of(here):
                far = lk.peer(here).node
                if (
                    lk.mode == "trunk"
                    and at.vlan in lk.vlans
                    and self.intent.nodes[far].role == "switch"
                    and far not in switches
                ):
                    switches.add(far)
                    todo.append(far)
        return sorted(
            h.name
            for h in self.intent.by_role("host")
            if h.name != host
            and (other := self.attachment(h.name)) is not None
            and other.switch in switches
            and other.vlan == at.vlan
        )

    def vlan_gateway(self, host: str) -> tuple[str, str] | None:
        """(router, address) of the gateway in `host`'s VLAN; None if the VLAN has none."""
        at = self.attachment(host)
        vlan = self.intent.vlans.get(at.vlan) if at and at.vlan is not None else None
        if vlan is None or not vlan.gateway_node or not vlan.gateway_ip:
            return None
        return vlan.gateway_node, vlan.gateway_ip.split("/")[0]

    def reachable_pairs(self) -> list[tuple[str, str]]:
        return [(a, b) for a, b in self.intent.reachable]

    def isolated_pairs(self) -> list[tuple[str, str]]:
        return [(a, b) for a, b in self.intent.isolated]

    def failover(self) -> FailoverPlan:
        """Raises LookupError if the intent has no primary/backup pair to fail over."""
        intent = self.intent
        try:
            primary = intent.link_by_label("primary")
            backup = intent.link_by_label("backup")
        except KeyError as exc:
            raise LookupError("intent has no links labelled 'primary' and 'backup'") from exc
        far_lan = next((lk for lk in intent.links if lk.ospf_passive), None)
        if far_lan is None or not intent.reachable:
            raise LookupError("intent has no passive LAN or no reachable pair to send traffic")
        router = primary.a.node
        lan_side = far_lan.a if far_lan.a.ip else far_lan.b
        host_side = far_lan.b if lan_side is far_lan.a else far_lan.a
        primary_hop, backup_hop = primary.peer(router).ip, backup.peer(router).ip
        if not (primary_hop and backup_hop and lan_side.ip):
            raise LookupError("primary/backup links or the far LAN have no addresses")
        return FailoverPlan(
            router=router,
            primary_port=primary.endpoint(router).port,
            primary_hop=primary_hop.split("/")[0],
            backup_hop=backup_hop.split("/")[0],
            prefix=_network(lan_side.ip),
            src=intent.reachable[0][0],
            dst=host_side.node,
        )

    # ---- the generated file -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        neighbors: dict[str, list[dict[str, str]]] = {r: [] for r in self._routers}
        for a in self.adjacencies():
            neighbors[a.router].append(
                {
                    "neighbor": a.neighbor,
                    "router_id": a.router_id,
                    "local_port": a.local_port,
                    "link": a.link,
                    "state": "Full",
                }
            )
        return {
            "topology": self.intent.name,
            "intent_digest": self.intent.digest,
            "ospf_neighbors": neighbors,
            "routes": {r: sorted(self.routes(r)) for r in self._routers},
            "vlan_membership": {
                sw.name: {
                    p: {"mode": pv.mode, "vlans": list(pv.vlans)}
                    for p, pv in self.vlan_membership(sw.name).items()
                }
                for sw in self.intent.by_role("switch")
            },
            "reachable": [list(p) for p in self.intent.reachable],
            "isolated": [list(p) for p in self.intent.isolated],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def _ospf_prefixes(self) -> dict[str, set[str]]:
        """Prefixes each router originates into OSPF: the networks of its intended
        OSPF interfaces."""
        return {
            r: {_network(a) for i in self.ospf_interfaces(r).values() for a in i.addresses}
            for r in self._routers
        }
