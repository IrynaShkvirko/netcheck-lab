"""Intent model: load, validate and derive expected state (ADR-0001)."""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class IntentError(ValueError):
    """The intent file is invalid."""


@dataclass(frozen=True)
class Endpoint:
    node: str
    port: str
    ip: str | None = None


@dataclass(frozen=True)
class Link:
    a: Endpoint
    b: Endpoint
    mode: str | None = None  # access | trunk | None (routed)
    vlan: int | None = None
    vlans: tuple[int, ...] = ()
    ospf_cost: int | None = None
    ospf_passive: bool = False
    label: str | None = None

    def endpoint(self, node: str) -> Endpoint:
        if self.a.node == node:
            return self.a
        if self.b.node == node:
            return self.b
        raise KeyError(node)

    def peer(self, node: str) -> Endpoint:
        return self.b if self.a.node == node else self.a

    @property
    def routed(self) -> bool:
        return self.mode is None


@dataclass(frozen=True)
class Node:
    name: str
    role: str  # router | switch | host
    router_id: str | None = None
    ip: str | None = None
    gateway: str | None = None


@dataclass(frozen=True)
class Vlan:
    vid: int
    name: str
    gateway_node: str | None = None
    gateway_ip: str | None = None


@dataclass(frozen=True)
class OspfSettings:
    area: int = 0
    network: str = "point-to-point"
    hello_interval: int = 1
    dead_interval: int = 4


@dataclass
class Intent:
    name: str
    ospf: OspfSettings
    nodes: dict[str, Node]
    vlans: dict[int, Vlan]
    links: list[Link]
    reachable: list[tuple[str, str]]
    isolated: list[tuple[str, str]]
    digest: str = ""
    source: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # ---- lookups -------------------------------------------------------
    def by_role(self, role: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.role == role]

    def links_of(self, node: str) -> list[Link]:
        return [lk for lk in self.links if node in (lk.a.node, lk.b.node)]

    def link_by_label(self, label: str) -> Link:
        for lk in self.links:
            if lk.label == label:
                return lk
        raise KeyError(f"no link labelled {label!r}")

    def host_ip(self, node: str) -> str:
        ip = self.nodes[node].ip
        if ip is None:
            raise IntentError(f"{node} has no ip")
        return ip.split("/")[0]

    def timer_vars(self) -> dict[str, float]:
        """Values that timing.yaml expressions may reference (ADR-0004)."""
        return {
            "hello_interval": float(self.ospf.hello_interval),
            "dead_interval": float(self.ospf.dead_interval),
        }


def _endpoint(d: dict[str, Any]) -> Endpoint:
    return Endpoint(node=d["node"], port=d["port"], ip=d.get("ip"))


def load_intent(path: str | Path) -> Intent:
    path = Path(path)
    text = path.read_text()
    raw = yaml.safe_load(text)
    nodes = {
        name: Node(
            name=name,
            role=d["role"],
            router_id=d.get("router_id"),
            ip=d.get("ip"),
            gateway=d.get("gateway"),
        )
        for name, d in raw["nodes"].items()
    }
    vlans = {}
    for vid, d in (raw.get("vlans") or {}).items():
        gw = d.get("gateway") or {}
        vlans[int(vid)] = Vlan(int(vid), d["name"], gw.get("node"), gw.get("ip"))
    links = [
        Link(
            a=_endpoint(d["a"]),
            b=_endpoint(d["b"]),
            mode=d.get("mode"),
            vlan=d.get("vlan"),
            vlans=tuple(d.get("vlans", ())),
            ospf_cost=d.get("ospf_cost"),
            ospf_passive=bool(d.get("ospf_passive")),
            label=d.get("label"),
        )
        for d in raw["links"]
    ]
    expect = raw.get("expect") or {}
    intent = Intent(
        name=raw["name"],
        ospf=OspfSettings(**(raw.get("ospf") or {})),
        nodes=nodes,
        vlans=vlans,
        links=links,
        reachable=[tuple(p) for p in expect.get("reachable", [])],
        isolated=[tuple(p) for p in expect.get("isolated", [])],
        digest=hashlib.sha256(text.encode()).hexdigest()[:12],
        source=path,
        raw=raw,
    )
    validate(intent)
    return intent


def validate(intent: Intent) -> None:
    errors: list[str] = []
    for lk in intent.links:
        for ep in (lk.a, lk.b):
            if ep.node not in intent.nodes:
                errors.append(f"link references unknown node {ep.node}")
        if lk.mode == "access" and lk.vlan not in intent.vlans:
            errors.append(f"access link {lk.a.node}-{lk.b.node} uses unknown VLAN {lk.vlan}")
        for vid in lk.vlans:
            if vid not in intent.vlans:
                errors.append(f"trunk {lk.a.node}-{lk.b.node} carries unknown VLAN {vid}")
        if lk.routed:
            ips = [ep.ip for ep in (lk.a, lk.b) if ep.ip]
            if len(ips) == 2:
                na = ipaddress.ip_interface(ips[0]).network
                nb = ipaddress.ip_interface(ips[1]).network
                if na != nb:
                    errors.append(f"routed link {lk.a.node}-{lk.b.node}: {na} != {nb}")
    seen: set[tuple[str, str]] = set()
    for lk in intent.links:
        for ep in (lk.a, lk.b):
            key = (ep.node, ep.port)
            if key in seen:
                errors.append(f"port {ep.node}:{ep.port} used twice")
            seen.add(key)
    for a, b in intent.reachable + intent.isolated:
        for n in (a, b):
            if n not in intent.nodes or intent.nodes[n].role != "host":
                errors.append(f"expectation references non-host {n}")
    if errors:
        raise IntentError("; ".join(errors))


# ---- expected state (the third generated output) -----------------------


def ospf_prefixes(intent: Intent) -> dict[str, set[str]]:
    """Prefixes each router originates into OSPF."""
    out: dict[str, set[str]] = {r.name: set() for r in intent.by_role("router")}
    for lk in intent.links:
        if lk.routed:
            for ep in (lk.a, lk.b):
                if ep.node in out and ep.ip:
                    out[ep.node].add(str(ipaddress.ip_interface(ep.ip).network))
    for vlan in intent.vlans.values():
        if vlan.gateway_node and vlan.gateway_ip:
            out[vlan.gateway_node].add(str(ipaddress.ip_interface(vlan.gateway_ip).network))
    return out


def expected_state(intent: Intent) -> dict[str, Any]:
    routers = {r.name for r in intent.by_role("router")}
    neighbors: dict[str, list[dict[str, str]]] = {r: [] for r in routers}
    for lk in intent.links:
        if lk.routed and lk.a.node in routers and lk.b.node in routers:
            for me, peer in ((lk.a, lk.b), (lk.b, lk.a)):
                neighbors[me.node].append(
                    {
                        "neighbor": peer.node,
                        "router_id": intent.nodes[peer.node].router_id or "",
                        "local_port": me.port,
                        "link": lk.label or f"{me.port}-{peer.port}",
                        "state": "Full",
                    }
                )
    originated = ospf_prefixes(intent)
    routes: dict[str, list[str]] = {}
    for r in routers:
        learned = set().union(*(p for o, p in originated.items() if o != r)) - originated[r]
        routes[r] = sorted(learned)
    vlan_membership: dict[str, dict[str, dict[str, Any]]] = {}
    for sw in intent.by_role("switch"):
        ports: dict[str, dict[str, Any]] = {}
        for lk in intent.links_of(sw.name):
            ep = lk.endpoint(sw.name)
            if lk.mode == "access":
                ports[ep.port] = {"mode": "access", "vlans": [lk.vlan]}
            elif lk.mode == "trunk":
                ports[ep.port] = {"mode": "trunk", "vlans": sorted(lk.vlans)}
        vlan_membership[sw.name] = dict(sorted(ports.items()))
    return {
        "topology": intent.name,
        "intent_digest": intent.digest,
        "ospf_neighbors": {k: neighbors[k] for k in sorted(neighbors)},
        "routes": {k: routes[k] for k in sorted(routes)},
        "vlan_membership": vlan_membership,
        "reachable": [list(p) for p in intent.reachable],
        "isolated": [list(p) for p in intent.isolated],
    }
