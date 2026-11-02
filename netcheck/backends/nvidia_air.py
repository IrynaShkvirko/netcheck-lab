"""NVIDIA backend: Cumulus Linux on NVIDIA Air, managed over the NVUE REST API.

STATUS: skeleton. The device side (NVUE REST transport, capability shapes,
revision-based snapshots) is laid out; the NVUE object paths marked UNVERIFIED
must be checked against the NVUE API reference for the Cumulus release in use,
and Air simulation creation is not implemented yet (ADR-0001 open question).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from netcheck.backends.base import Backend
from netcheck.capabilities import Bridge, Links, Ospf, OspfNeighbor, PortVlans, Route
from netcheck.device import Device
from netcheck.devicelog import DeviceLog
from netcheck.intent import Intent
from netcheck.render import port_name
from netcheck.transport.base import TransportError
from netcheck.transport.nvue_rest import NvueRestTransport

# UNVERIFIED: confirm each path in the NVUE API reference before relying on it.
PATHS = {
    "ospf_neighbors": "vrf/default/router/ospf/neighbor",
    "routes": "vrf/default/router/rib/ipv4/route",
    "bridge_vlans": "bridge/domain/br_default",
    "interface": "interface/{ifname}",
    "revision": "revision",
}


class _NvueBase:
    def __init__(self, t: NvueRestTransport, port_map: dict[str, str]) -> None:
        self.t = t
        self.port_map = port_map
        self.to_logical = {v: k for k, v in port_map.items()}

    def get(self, path: str) -> Any:
        res = self.t.request("GET", path, params={"rev": "operational"})
        if not res.ok:
            raise TransportError(f"GET {path}: HTTP {res.status}")
        return res.body


class NvueOspf(_NvueBase, Ospf):
    def neighbors(self) -> list[OspfNeighbor]:
        raise NotImplementedError("map NVUE OSPF neighbor JSON to OspfNeighbor (UNVERIFIED path)")

    def routes(self) -> dict[str, Route]:
        raise NotImplementedError("map NVUE RIB JSON to Route (UNVERIFIED path)")

    def lsdb_prefixes(self) -> set[str]:
        raise NotImplementedError

    def running_config(self) -> str:
        res = self.t.request("GET", "", params={"rev": "applied"})
        return str(res.body)


class NvueBridge(_NvueBase, Bridge):
    def vlan_membership(self) -> dict[str, PortVlans]:
        raise NotImplementedError("map NVUE bridge domain JSON to PortVlans (UNVERIFIED path)")

    def set_access_vlan(self, port: str, vlan: int) -> None:
        raise NotImplementedError


class NvueLinks(_NvueBase, Links):
    def set_link(self, port: str, up: bool) -> None:
        raise NotImplementedError("PATCH link state inside a revision, then apply")

    def link_state(self, port: str) -> dict[str, str]:
        raise NotImplementedError


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
            self._devices = {}
            for name, node in self.intent.nodes.items():
                if node.role == "host":
                    continue  # hosts are reached over SSH via Air's OOB network (TODO)
                log = DeviceLog(name)
                t = NvueRestTransport(
                    self.mgmt[name],
                    self.settings.get("nvue_user", "cumulus"),
                    self.password,
                    log,
                    port=self.settings.get("nvue_port", 8765),
                )
                ports = {
                    ep.port: port_name(ep.port, self.prefix)
                    for lk in self.intent.links_of(name)
                    for ep in (lk.a, lk.b)
                    if ep.node == name
                }
                caps: dict[type, object] = {Links: NvueLinks(t, ports)}
                caps[Ospf if node.role == "router" else Bridge] = (
                    NvueOspf(t, ports) if node.role == "router" else NvueBridge(t, ports)
                )
                self._devices[name] = Device(name, node.role, self.name, log, caps, ports)
        return self._devices

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
