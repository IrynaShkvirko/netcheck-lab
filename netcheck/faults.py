"""Deliberate faults for the demo. Each one breaks a single intent.

Remove them with `make lab-reset` (restores the baseline snapshot).
"""

from __future__ import annotations

from collections.abc import Callable

from netcheck.assertions import host_attachment
from netcheck.backends.base import Backend
from netcheck.backends.local import FrrOspf


def wrong_vlan(backend: Backend) -> str:
    """Move the first isolated host's peer into the other VLAN."""
    intent = backend.intent
    a, b = intent.isolated[0]
    _, _, vlan_a = host_attachment(intent, a)
    sw, port_b, vlan_b = host_attachment(intent, b)
    assert vlan_a is not None
    backend.devices()[sw].bridge.set_access_vlan(port_b, vlan_a)
    return f"{sw}:{port_b} ({b}) moved from VLAN {vlan_b} to VLAN {vlan_a}"


def missing_ospf_network(backend: Backend) -> str:
    """Take the passive LAN behind a router out of OSPF; adjacencies stay Full."""
    intent = backend.intent
    for lk in intent.links:
        if lk.ospf_passive:
            ep = lk.a if intent.nodes[lk.a.node].role == "router" else lk.b
            dev = backend.devices()[ep.node]
            ospf = dev.ospf
            if not isinstance(ospf, FrrOspf):
                raise NotImplementedError("fault implemented for the local backend only")
            ifname = dev.port_map[ep.port]
            ospf.run(
                f"vtysh -c 'conf t' -c 'interface {ifname}' -c 'no ip ospf area {intent.ospf.area}'"
            ).check()
            return (
                f"{ep.node}:{ep.port} ({ifname}, {ep.ip}) removed from OSPF area {intent.ospf.area}"
            )
    raise LookupError("intent has no passive OSPF link")


FAULTS: dict[str, Callable[[Backend], str]] = {
    "wrong-vlan": wrong_vlan,
    "missing-ospf-network": missing_ospf_network,
}
