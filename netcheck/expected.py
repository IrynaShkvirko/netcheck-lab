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

from netcheck.capabilities import PortVlans
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
class AdvertisingInterface:
    """A router interface that must carry `address` and be in OSPF: a routed port (p3)
    or a VLAN gateway sub-interface (p1.10)."""

    name: str  # logical interface
    port: str  # logical port
    vlan: int | None
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

    def next_hops(self, router: str, peer: str) -> set[str]:
        """`peer`'s addresses (no prefix length) on the routed links it shares with
        `router`: the next hops `router` may use towards `peer`."""
        out = set()
        for lk in self.intent.links_of(router):
            far = lk.peer(router)
            if lk.routed and far.node == peer and far.ip:
                out.add(far.ip.split("/")[0])
        return out

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

    def advertising_interfaces(self, router: str, prefix: str) -> list[AdvertisingInterface]:
        """The interfaces of `router` that put `prefix` into OSPF: a routed port in that
        network, or the gateway sub-interface of a VLAN with that network."""
        out = []
        for lk in self.intent.links_of(router):
            ep = lk.endpoint(router)
            if ep.ip and _network(ep.ip) == prefix:
                out.append(AdvertisingInterface(ep.port, ep.port, None, ep.ip))
        for vid, vlan in self.intent.vlans.items():
            if vlan.gateway_node != router or not vlan.gateway_ip:
                continue
            if _network(vlan.gateway_ip) != prefix:
                continue
            for lk in self.intent.links_of(router):
                if lk.mode == "trunk" and vid in lk.vlans:
                    port = lk.endpoint(router).port
                    out.append(AdvertisingInterface(f"{port}.{vid}", port, vid, vlan.gateway_ip))
        return out

    # ---- hosts ------------------------------------------------------------------------
    def attachment(self, host: str) -> Attachment | None:
        """Where a host is plugged into a switch; None if it hangs off a router."""
        for lk in self.intent.links_of(host):
            peer = lk.peer(host)
            if self.intent.nodes[peer.node].role == "switch":
                return Attachment(peer.node, peer.port, lk.vlan)
        return None

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
        """Prefixes each router originates into OSPF."""
        out: dict[str, set[str]] = {r: set() for r in self._routers}
        for lk in self.intent.links:
            if lk.routed:
                for ep in (lk.a, lk.b):
                    if ep.node in out and ep.ip:
                        out[ep.node].add(_network(ep.ip))
        for vlan in self.intent.vlans.values():
            if vlan.gateway_node and vlan.gateway_ip:
                out[vlan.gateway_node].add(_network(vlan.gateway_ip))
        return out
