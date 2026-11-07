"""Deliberate faults. Each one breaks a single intent.

Used by the demo (`make demo`) and for trying out failures by hand.
Remove a fault with `make lab-reset` (restores the baseline snapshot).
"""

from __future__ import annotations

from collections.abc import Callable

from netcheck.backends.base import Backend
from netcheck.expected import Expected


def wrong_vlan(backend: Backend) -> str:
    """Move the first isolated host's peer into the other VLAN."""
    intent = backend.intent
    if not intent.isolated:
        raise LookupError("wrong-vlan needs an isolated pair in the intent (expect.isolated)")
    a, b = intent.isolated[0]
    expected = Expected(intent)
    at_a, at_b = expected.attachment(a), expected.attachment(b)
    if at_a is None or at_b is None or at_a.vlan is None:
        raise LookupError(f"wrong-vlan needs {a} and {b} on switch access ports")
    backend.devices()[at_b.switch].bridge.set_access_vlan(at_b.port, at_a.vlan)
    return f"{at_b.switch}:{at_b.port} ({b}) moved from VLAN {at_b.vlan} to VLAN {at_a.vlan}"


def missing_ospf_network(backend: Backend) -> str:
    """Take the passive LAN behind a router out of OSPF; adjacencies stay Full."""
    intent = backend.intent
    for lk in intent.links:
        if lk.ospf_passive:
            ep = lk.a if intent.nodes[lk.a.node].role == "router" else lk.b
            dev = backend.devices()[ep.node]
            dev.ospf.set_interface_area(ep.port, None)
            real = dev.port_map.get(ep.port, ep.port)
            return (
                f"{ep.node}:{ep.port} ({real}, {ep.ip}) removed from OSPF area {intent.ospf.area}"
            )
    raise LookupError("missing-ospf-network needs a passive OSPF link in the intent")


FAULTS: dict[str, Callable[[Backend], str]] = {
    "wrong-vlan": wrong_vlan,
    "missing-ospf-network": missing_ospf_network,
}
