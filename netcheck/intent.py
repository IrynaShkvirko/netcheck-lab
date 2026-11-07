"""Intent model: load, validate and derive expected state (ADR-0001)."""

from __future__ import annotations

import hashlib
import ipaddress
import re
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

    def link_at(self, node: str, port: str) -> Link:
        for lk in self.links_of(node):
            if lk.endpoint(node).port == port:
                return lk
        raise KeyError(f"no link at {node}:{port}")

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


ROLES = ("router", "switch", "host")
_PORT = re.compile(r"^p\d+$")


def _iface(value: str | None, where: str, errors: list[str]) -> ipaddress.IPv4Interface | None:
    """Parse 'a.b.c.d/len'; record an error instead of raising."""
    if value is None:
        return None
    try:
        iface = ipaddress.ip_interface(value)
    except ValueError:
        errors.append(f"{where}: {value!r} is not an address with prefix length (a.b.c.d/len)")
        return None
    if not isinstance(iface, ipaddress.IPv4Interface) or "/" not in value:
        errors.append(f"{where}: {value!r} must be IPv4 with a prefix length")
        return None
    return iface


def validate(intent: Intent) -> None:
    """Reject an intent that cannot describe a real lab, with every problem at once.

    Values from the intent end up in device commands, so addresses and port names
    are checked strictly here rather than failing later with a cryptic error.
    """
    errors: list[str] = []
    o = intent.ospf
    if o.hello_interval <= 0 or o.dead_interval <= o.hello_interval:
        errors.append(
            "ospf: need 0 < hello_interval < dead_interval, "
            f"got {o.hello_interval}/{o.dead_interval}"
        )
    if o.area < 0:
        errors.append(f"ospf: area must be >= 0, got {o.area}")

    router_ids: dict[str, str] = {}
    for n in intent.nodes.values():
        if n.role not in ROLES:
            errors.append(f"node {n.name}: role {n.role!r} is not one of {', '.join(ROLES)}")
        if n.role == "router":
            try:
                rid = str(ipaddress.IPv4Address(n.router_id or ""))
            except ValueError:
                errors.append(f"router {n.name}: router_id {n.router_id!r} is not an IPv4 address")
            else:
                if rid in router_ids:
                    errors.append(
                        f"router {n.name}: router_id {rid} also used by {router_ids[rid]}"
                    )
                router_ids[rid] = n.name
        if n.role == "host":
            iface = _iface(n.ip, f"host {n.name} ip", errors)
            if n.ip is None:
                errors.append(f"host {n.name}: needs an ip")
            if n.gateway is not None:
                try:
                    gw = ipaddress.IPv4Address(n.gateway)
                except ValueError:
                    errors.append(f"host {n.name}: gateway {n.gateway!r} is not an IPv4 address")
                else:
                    if iface is not None and gw not in iface.network:
                        errors.append(f"host {n.name}: gateway {gw} is outside {iface.network}")
            links = intent.links_of(n.name)
            if len(links) != 1:
                errors.append(f"host {n.name}: must have exactly one link, has {len(links)}")

    for vid, vlan in intent.vlans.items():
        if not 1 <= vid <= 4094:
            errors.append(f"vlan {vid}: id must be 1-4094")
        if vlan.gateway_node is not None:
            gw_node = intent.nodes.get(vlan.gateway_node)
            if gw_node is None or gw_node.role != "router":
                errors.append(f"vlan {vid}: gateway node {vlan.gateway_node} is not a router")
            _iface(vlan.gateway_ip, f"vlan {vid} gateway ip", errors)

    seen: set[tuple[str, str]] = set()
    for lk in intent.links:
        where = f"link {lk.a.node}:{lk.a.port}-{lk.b.node}:{lk.b.port}"
        for ep in (lk.a, lk.b):
            if ep.node not in intent.nodes:
                errors.append(f"{where}: unknown node {ep.node}")
            if not _PORT.match(ep.port):
                errors.append(f"{where}: port {ep.port!r} must look like p1, p2, ...")
            key = (ep.node, ep.port)
            if key in seen:
                errors.append(f"port {ep.node}:{ep.port} used twice")
            seen.add(key)
        if lk.mode not in (None, "access", "trunk"):
            errors.append(f"{where}: mode {lk.mode!r} must be access, trunk or absent (routed)")
        if lk.mode == "access" and lk.vlan not in intent.vlans:
            errors.append(f"access link {lk.a.node}-{lk.b.node} uses unknown VLAN {lk.vlan}")
        for vid in lk.vlans:
            if vid not in intent.vlans:
                errors.append(f"trunk {lk.a.node}-{lk.b.node} carries unknown VLAN {vid}")
        if lk.ospf_cost is not None and (not isinstance(lk.ospf_cost, int) or lk.ospf_cost < 1):
            errors.append(f"{where}: ospf_cost must be a positive integer, got {lk.ospf_cost!r}")
        if lk.routed:
            ifaces = [
                _iface(ep.ip, f"{where} {ep.node} ip", errors) for ep in (lk.a, lk.b) if ep.ip
            ]
            nets = [i.network for i in ifaces if i is not None]
            if len(nets) == 2 and nets[0] != nets[1]:
                errors.append(f"routed link {lk.a.node}-{lk.b.node}: {nets[0]} != {nets[1]}")

    labels = [lk.label for lk in intent.links if lk.label]
    for label in sorted({x for x in labels if labels.count(x) > 1}):
        errors.append(f"link label {label!r} used more than once")

    for a, b in intent.reachable + intent.isolated:
        if a == b:
            errors.append(f"expectation pair [{a}, {b}] names the same host twice")
        for host in (a, b):
            if host not in intent.nodes or intent.nodes[host].role != "host":
                errors.append(f"expectation references non-host {host}")
    if errors:
        raise IntentError("; ".join(errors))
