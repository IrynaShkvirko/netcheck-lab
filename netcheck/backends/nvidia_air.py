"""NVIDIA backend: Cumulus Linux on NVIDIA DSX Air, managed over the NVUE REST API.

STATUS
- Device side (this file, top half): NVUE responses are parsed into the same normalized
  types as the local backend, and config changes go through the NVUE revision flow.
  Unit-tested against JSON samples in tests/unit/fixtures/nvue/. The samples follow the
  NVUE object model and the JSON examples in the NVUE command reference; the RIB, bridge
  and link samples are assembled from the documented fields, not captured from a switch.
  Replace them with real responses (`record_samples`) once a simulation is available.
- Lab side (NvidiaAirBackend): creating the DSX Air simulation, reaching NVUE through the
  oob-mgmt-server, host SSH and revision-based snapshots are not implemented yet
  (ADR-0001, ADR-0003 open questions).

Parsers are strict: a response shape they do not recognize raises NvueShapeError
instead of returning empty state, so a schema change fails loudly and points here.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from netcheck.assembly import CapabilityFactory, DeviceSpec, assemble
from netcheck.backends.base import Backend
from netcheck.capabilities import (
    Bridge,
    Links,
    Ospf,
    OspfInterface,
    OspfNeighbor,
    PortVlans,
    Route,
)
from netcheck.device import Device
from netcheck.intent import Intent
from netcheck.ports import PortMap
from netcheck.transport.base import TransportError
from netcheck.transport.nvue_rest import NvueRestTransport

BRIDGE_DOMAIN = "br_default"

# GET paths (under /nvue_v1). rev=operational: live state; rev=applied: configuration.
PATHS = {
    "ospf_neighbors": "vrf/default/router/ospf/neighbor",
    "routes": "vrf/default/router/rib/ipv4/route",
    "interfaces": "interface",
    "bridge_domain": f"bridge/domain/{BRIDGE_DOMAIN}",
    "system": "system",
}


class NvueShapeError(ValueError):
    """An NVUE response did not have the shape the parser expects."""


def _dict(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NvueShapeError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def _enum(value: Any) -> str:
    """NVUE writes some enums as {"up": {}}; others as "up"."""
    if isinstance(value, dict) and len(value) == 1:
        return str(next(iter(value)))
    return "" if value is None else str(value)


def _vlan_ids(spec: Any) -> tuple[int, ...]:
    """NVUE VLAN lists: {"10": {}, "20-22": {}} or "10,20-22" -> (10, 20, 21, 22)."""
    keys = spec.keys() if isinstance(spec, dict) else str(spec).split(",")
    vids: set[int] = set()
    for key in keys:
        part = str(key).strip()
        if not part:
            continue
        lo, dash, hi = part.partition("-")
        if not lo.isdigit() or (dash and not hi.isdigit()):
            raise NvueShapeError(f"VLAN list: cannot read {part!r}")
        vids.update(range(int(lo), int(hi or lo) + 1))
    return tuple(sorted(vids))


# ---- parsers (NVUE JSON -> normalized types) ----------------------------------------


_NVUE_STATES = {"full": "Full", "2-way": "2-Way", "twoway": "2-Way", "init": "Init"}


def parse_nvue_neighbors(data: Any, ports: PortMap) -> list[OspfNeighbor]:
    """GET vrf/default/router/ospf/neighbor (rev=operational):
    {"<router-id>": {"interface": {"swp51": {"local-ip": {"10.0.1.0": {"state": "full"}}}}}}
    One OspfNeighbor per neighbor and interface; state spelled like FRR ("Full")."""
    out = []
    for rid, nbr in _dict(data, "ospf neighbors").items():
        for ifname, iface in _dict(_dict(nbr, rid).get("interface"), f"{rid} interface").items():
            local_ips = _dict(_dict(iface, ifname).get("local-ip"), f"{rid} {ifname} local-ip")
            if not local_ips:
                raise NvueShapeError(f"ospf neighbor {rid} on {ifname}: no local-ip entry")
            for entry in local_ips.values():
                raw = str(_dict(entry, "local-ip entry").get("state", "unknown"))
                state = _NVUE_STATES.get(raw.lower(), raw.capitalize())
                out.append(OspfNeighbor(rid, state, ports.logical(ifname)))
    return out


def parse_nvue_routes(data: Any, protocol: str | None = None) -> dict[str, Route]:
    """GET vrf/default/router/rib/ipv4/route (rev=operational), the route/protocol/
    entry-index/via tree of the NVUE command reference (Cumulus Linux before 5.11):
    {"10.0.30.0/24": {"protocol": {"ospf": {"entry-index": {"1": {
        "flags": {"installed": {}, "selected": {}}, "via": {"10.0.12.1": {...}}}}}}}}
    Keeps the selected entry of each prefix, with every next hop listed under "via";
    `protocol="ospf"` then keeps only prefixes OSPF won (like `show ip route ospf`).
    Cumulus 5.11+ dropped this tree from the CLI; its shape is not documented yet, so it
    raises NvueShapeError until a captured sample exists."""
    data = _dict(data, "routes")
    data = _dict(data.get("route", data), "routes")
    out: dict[str, Route] = {}
    for prefix, entry in data.items():
        protocols = _dict(entry, prefix).get("protocol")
        if not isinstance(protocols, dict):
            raise NvueShapeError(
                f"route {prefix}: no protocol/entry-index tree (Cumulus 5.11+ format?); "
                "capture a sample with record_samples() and extend parse_nvue_routes"
            )
        for proto, pdata in protocols.items():
            entries = _dict(_dict(pdata, f"{prefix} {proto}").get("entry-index"), "entry-index")
            for e in entries.values():
                e = _dict(e, f"{prefix} entry")
                flags = set(_dict(e.get("flags"), "flags"))
                if not flags & {"selected", "installed", "fib-selected"}:
                    continue
                hops = tuple(sorted(_dict(e.get("via"), f"{prefix} via")))
                out[prefix] = Route(prefix, proto, hops, "installed" in flags)
    if protocol is not None:
        # the selected entry decides: an OSPF entry that lost to "connected" is not kept
        out = {k: r for k, r in out.items() if r.protocol == protocol}
    return dict(sorted(out.items()))


def parse_nvue_ospf_interfaces(interfaces: Any, ports: PortMap) -> dict[str, OspfInterface]:
    """GET interface (rev=applied): OSPF settings as configured, like the FRR running
    config on the local backend. Per interface:
    {"ip": {"address": {"10.0.12.0/31": {}}}, "router": {"ospf": {"area": "0",
     "timers": {"hello-interval": 1, "dead-interval": 4}, "network-type": "point-to-point",
     "passive": "on", "cost": 10}}}
    Only interfaces with an OSPF section are returned; timers are in seconds."""
    out: dict[str, OspfInterface] = {}
    for ifname, cfg in _dict(interfaces, "interfaces").items():
        cfg = _dict(cfg, ifname)
        ospf = _dict(_dict(cfg.get("router"), f"{ifname} router").get("ospf"), f"{ifname} ospf")
        if not ospf:
            continue
        name = ports.logical(ifname)
        timers = _dict(ospf.get("timers"), f"{ifname} ospf timers")
        addresses = _dict(_dict(cfg.get("ip"), f"{ifname} ip").get("address"), "address")

        def num(value: Any) -> int | None:
            return int(value) if value is not None and str(value).isdigit() else None

        area = ospf.get("area")
        out[name] = OspfInterface(
            name=name,
            addresses=tuple(sorted(addresses)),
            area=None if area is None else str(area),
            hello_interval=num(timers.get("hello-interval")),
            dead_interval=num(timers.get("dead-interval")),
            passive=_enum(ospf.get("passive")) == "on",
            cost=num(ospf.get("cost")),
            network=_enum(ospf.get("network-type")) or None,
        )
    return dict(sorted(out.items()))


def parse_nvue_bridge(
    interfaces: Any, domain: Any, ports: PortMap, name: str = BRIDGE_DOMAIN
) -> dict[str, PortVlans]:
    """GET interface and bridge/domain/br_default (rev=operational) -> {port: PortVlans}.

    A port is in the bridge when it has bridge/domain/<name>. Then:
      "access": 10              -> access VLAN 10
      "vlan": {"10": {}, ...}   -> trunk with those VLANs ("all" means every domain VLAN)
      neither                   -> trunk with every VLAN of the domain (the NVUE default)
    Ports the intent does not map keep their real name (swp9), as on the local backend.
    """
    domain_vlans = _vlan_ids(_dict(domain, "bridge domain").get("vlan") or {})
    out: dict[str, PortVlans] = {}
    for ifname, cfg in _dict(interfaces, "interfaces").items():
        bridge = _dict(_dict(cfg, ifname).get("bridge"), f"{ifname} bridge")
        membership = _dict(bridge.get("domain"), f"{ifname} bridge domain").get(name)
        if membership is None:
            continue
        membership = _dict(membership, f"{ifname} {name}")
        port = ports.logical(ifname)
        if membership.get("access") not in (None, "", {}):
            out[port] = PortVlans("access", (int(_enum(membership["access"])),))
            continue
        vlan = membership.get("vlan")
        vids = domain_vlans if vlan in (None, "all") or "all" in (vlan or {}) else _vlan_ids(vlan)
        out[port] = PortVlans("trunk", vids) if vids else PortVlans("none", ())
    return dict(sorted(out.items()))


def parse_nvue_link(port: str, ifname: str, iface: Any) -> dict[str, str]:
    """One interface (rev=operational) -> the link dict the local backend returns:
    {"port", "ifname", "admin": up|down, "operstate": UP|DOWN}."""
    link = _dict(_dict(iface, ifname).get("link"), f"{ifname} link")
    if not link:
        raise NvueShapeError(f"{ifname}: no link section")
    admin = str(link.get("admin-status") or _enum(link.get("state")) or "?").lower()
    oper = str(link.get("oper-status") or "?").upper()
    return {"port": port, "ifname": ifname, "admin": admin, "operstate": oper}


# ---- config changes: the NVUE revision flow -----------------------------------------


Change = tuple[str, str, Any]  # (method, path, body): PATCH merges, DELETE removes


def apply_changes(
    t: NvueRestTransport,
    changes: list[Change],
    *,
    timeout: float = 60.0,
    poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Create a revision, put the changes into it, apply it, wait until it is applied.
    Returns the revision ID. Any HTTP error or a failed apply raises TransportError."""
    res = t.request("POST", "revision")
    if not res.ok or not isinstance(res.body, dict) or len(res.body) != 1:
        raise TransportError(f"create revision: HTTP {res.status} {res.body!r}")
    rev = next(iter(res.body))
    for method, path, body in changes:
        res = t.request(method, path, body=body, params={"rev": rev})
        if not res.ok and not (method == "DELETE" and res.status == 404):
            raise TransportError(
                f"{method} {path} (revision {rev}): HTTP {res.status} {res.body!r}"
            )
    res = t.request(
        "PATCH", f"revision/{rev}", body={"state": "apply", "auto-prompt": {"ays": "ays_yes"}}
    )
    if not res.ok:
        raise TransportError(f"apply revision {rev}: HTTP {res.status} {res.body!r}")
    deadline = time.monotonic() + timeout
    while True:
        res = t.request("GET", f"revision/{rev}")
        state = res.body.get("state") if isinstance(res.body, dict) else None
        if state == "applied":
            return str(rev)
        if state in ("invalid", "apply_fail", "apply_error", "ays_fail", "ignore_fail"):
            raise TransportError(f"revision {rev} failed to apply: {res.body!r}")
        if time.monotonic() > deadline:
            raise TransportError(f"revision {rev} not applied after {timeout:.0f}s: {res.body!r}")
        sleep(poll)


# ---- capabilities -------------------------------------------------------------------


class _NvueBase:
    def __init__(self, t: NvueRestTransport, port_map: PortMap) -> None:
        self.t = t
        self.port_map = port_map

    def get(self, path: str, rev: str = "operational") -> Any:
        res = self.t.request("GET", path, params={"rev": rev})
        if not res.ok:
            raise TransportError(f"GET {path} (rev={rev}): HTTP {res.status}")
        return res.body

    def apply(self, changes: list[Change]) -> str:
        return apply_changes(self.t, changes)


class NvueOspf(_NvueBase, Ospf):
    def neighbors(self) -> list[OspfNeighbor]:
        return parse_nvue_neighbors(self.get(PATHS["ospf_neighbors"]), self.port_map)

    def routes(self) -> dict[str, Route]:
        return parse_nvue_routes(self.get(PATHS["routes"]), protocol="ospf")

    def lsdb_prefixes(self) -> set[str]:
        # only used as evidence; failures there are reported, never fatal
        raise NotImplementedError("NVUE LSDB path not mapped yet")

    def interface_settings(self) -> dict[str, OspfInterface]:
        return parse_nvue_ospf_interfaces(self.get(PATHS["interfaces"], "applied"), self.port_map)

    def set_interface_area(self, port: str, area: int | None) -> None:
        path = f"interface/{self.port_map.real(port)}/router/ospf"
        if area is None:
            self.apply([("DELETE", f"{path}/area", None)])
        else:
            self.apply([("PATCH", path, {"area": str(area)})])


class NvueBridge(_NvueBase, Bridge):
    def vlan_membership(self) -> dict[str, PortVlans]:
        return parse_nvue_bridge(
            self.get(PATHS["interfaces"]), self.get(PATHS["bridge_domain"]), self.port_map
        )

    def set_access_vlan(self, port: str, vlan: int) -> None:
        path = f"interface/{self.port_map[port]}/bridge/domain/{BRIDGE_DOMAIN}"
        # drop a trunk VLAN list first, or the port would end up with both
        self.apply([("DELETE", f"{path}/vlan", None), ("PATCH", path, {"access": vlan})])


class NvueLinks(_NvueBase, Links):
    def set_link(self, port: str, up: bool) -> None:
        state = "up" if up else "down"
        self.apply([("PATCH", f"interface/{self.port_map[port]}/link", {"state": {state: {}}})])

    def link_state(self, port: str) -> dict[str, str]:
        ifname = self.port_map[port]
        return parse_nvue_link(port, ifname, self.get(f"interface/{ifname}"))

    def link_states(self, ports: list[str]) -> dict[str, dict[str, str]]:
        """All requested ports from one GET of every interface."""
        interfaces = _dict(self.get(PATHS["interfaces"]), "interfaces")
        out = {}
        for port in ports:
            ifname = self.port_map[port]
            if ifname in interfaces:
                out[port] = parse_nvue_link(port, ifname, interfaces[ifname])
        return out


def record_samples(t: NvueRestTransport, dest: Path) -> list[Path]:
    """Save the responses the parsers read, from a real switch, as test fixtures:
    <dest>/<name>.<rev>.json. Run once against a DSX Air simulation, then point the unit
    tests at the captured files instead of the documentation-based samples."""
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name, path in PATHS.items():
        for rev in ("operational", "applied"):
            res = t.request("GET", path, params={"rev": rev})
            if res.ok:
                f = dest / f"{name}.{rev}.json"
                f.write_text(json.dumps(res.body, indent=2, sort_keys=True) + "\n")
                written.append(f)
    return written


class NvueFactory(CapabilityFactory):
    """Adapters of the NVIDIA backend: NVUE REST for router and switch links, OSPF and the
    bridge. Hosts (SSH via Air's OOB network) and revision snapshots are not built yet,
    so tests needing them are skipped (ADR-0001, ADR-0003 open questions)."""

    def __init__(self, open_transport: Callable[[DeviceSpec], NvueRestTransport]) -> None:
        self.open_transport = open_transport
        self._transports: dict[str, NvueRestTransport] = {}

    def _t(self, spec: DeviceSpec) -> NvueRestTransport:
        if spec.name not in self._transports:
            self._transports[spec.name] = self.open_transport(spec)
        return self._transports[spec.name]

    def links(self, spec: DeviceSpec) -> Links | None:
        return None if spec.role == "host" else NvueLinks(self._t(spec), spec.ports)

    def ospf(self, spec: DeviceSpec) -> Ospf:
        return NvueOspf(self._t(spec), spec.ports)

    def bridge(self, spec: DeviceSpec) -> Bridge:
        return NvueBridge(self._t(spec), spec.ports)


class NvidiaAirBackend(Backend):
    name = "nvidia"
    timing_profile = "nvidia"

    def __init__(self, intent: Intent, settings: dict[str, Any], root: Path) -> None:
        super().__init__(intent, settings, root)
        self.prefix = settings.get("port_prefix", "swp")
        self.token = os.environ.get("NVIDIA_AIR_TOKEN", "")
        self.password = os.environ.get("NVUE_PASSWORD", "")
        self._devices: dict[str, Device] | None = None
        self.mgmt: dict[str, str] = {}  # node -> management address, filled at deploy

    def _require_token(self) -> None:
        if not self.token:
            raise RuntimeError("NVIDIA backend needs NVIDIA_AIR_TOKEN and NVUE_PASSWORD")

    def deploy(self, fresh: bool = False) -> bool:
        self._require_token()
        raise NotImplementedError(
            "Create an Air simulation from the generated topology, wait for boot, "
            "record management addresses in self.mgmt (ADR-0001 open question)."
        )

    def destroy(self) -> None:
        raise NotImplementedError

    def is_running(self) -> bool:
        return bool(self.mgmt)

    def devices(self) -> dict[str, Device]:
        if self._devices is None:
            factory = NvueFactory(self.open_transport)
            self._devices = assemble(self.intent, self.name, self.prefix, factory)
        return self._devices

    def open_transport(self, spec: DeviceSpec) -> NvueRestTransport:
        return NvueRestTransport(
            self.mgmt[spec.name],
            self.settings.get("nvue_user", "cumulus"),
            self.password,
            spec.log,
            port=self.settings.get("nvue_port", 8765),
            verify_tls=self.settings.get("ca_bundle") or self.settings.get("verify_tls", True),
        )

    def snapshot(self, label: str) -> str:
        """NVUE: the currently applied revision is the snapshot (ADR-0003)."""
        raise NotImplementedError("read the applied revision ID per switch")

    def restore(self, snapshot_id: str) -> None:
        raise NotImplementedError("re-apply the recorded revision (ADR-0003 open question)")

    def environment(self) -> dict[str, str]:
        return {
            "topology": self.intent.name,
            "backend": self.name,
            "intent": self.intent.digest,
            "nos": "Cumulus Linux (NVUE)",
        }
