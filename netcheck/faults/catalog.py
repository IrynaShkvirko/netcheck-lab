"""Deliberate faults. Each one breaks a single intent.

Used by the demo (`make demo`), by `make fault-check` and for trying out failures by hand.
Remove a fault with `make lab-reset` (restores the baseline snapshot).

Every fault declares the lab tests that must catch it (`Fault.detected_by`), each with the
text its failure report must show. `python -m netcheck fault-check` injects each fault on
the running lab and holds the suite to that; the unit tests hold the declarations to the
fake lab for every intent. A fault cannot be added without saying what detects it.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass

from netcheck.backends.base import Backend
from netcheck.model.expected import Expected
from netcheck.model.intent import Intent


@dataclass(frozen=True)
class Detection:
    """A lab test that must fail while a fault is applied, and what its report must say."""

    file: str  # relative to the repo root: tests/lab/test_vlan.py
    test: str  # test name with its parameters: test_hosts_..._isolated[h1-h3]
    shows: tuple[str, ...]  # text the failure report must contain

    @property
    def node_id(self) -> str:
        return f"{self.file}::{self.test}"


@dataclass(frozen=True)
class Fault:
    apply: Callable[[Backend], str]  # breaks the lab, returns what it changed
    detected_by: Callable[[Intent], list[Detection]]  # the tests that must catch it


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


def wrong_vlan_detected_by(intent: Intent) -> list[Detection]:
    """The isolation test for the pair, naming the drifted port; the VLAN drift test."""
    a, b = intent.isolated[0]
    expected = Expected(intent)
    at_a, at_b = expected.attachment(a), expected.attachment(b)
    if at_a is None or at_b is None:
        raise LookupError(f"wrong-vlan needs {a} and {b} on switch access ports")
    where = f"{at_b.switch}:{at_b.port}"
    return [
        Detection(
            "tests/lab/test_vlan.py",
            f"test_hosts_in_different_vlans_are_isolated[{a}-{b}]",
            (
                f"INTENT VIOLATED: {a} (VLAN {at_a.vlan}) and {b} (VLAN {at_b.vlan}) "
                "must be isolated",
                f"{where}: expected 'access VLAN {at_b.vlan}', actual 'access VLAN {at_a.vlan}'",
            ),
        ),
        Detection(
            "tests/lab/test_drift.py",
            f"test_vlan_membership_matches_intent[{at_b.switch}]",
            (f"INTENT VIOLATED: {where} must be access VLAN {at_b.vlan}",),
        ),
    ]


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


def missing_ospf_network_detected_by(intent: Intent) -> list[Detection]:
    """Every router that must learn the LAN reports it missing, with the diagnosis that
    the origin stopped advertising it (adjacencies stay Full); the route drift test."""
    lk = next(lk for lk in intent.links if lk.ospf_passive)
    ep = lk.a if intent.nodes[lk.a.node].role == "router" else lk.b
    prefix = str(ipaddress.ip_interface(ep.ip or "").network)
    expected = Expected(intent)
    learners = [r for r in expected.routers() if prefix in expected.routes(r)]
    return [
        Detection(
            "tests/lab/test_ospf.py",
            f"test_ospf_route_is_learned[{r}-{prefix}]",
            (
                f"INTENT VIOLATED: {r} must learn {prefix} via OSPF from {ep.node}",
                f"{r}: adjacency Full, but {ep.node} is not advertising {prefix}",
            ),
        )
        for r in learners
    ] + [
        Detection(
            "tests/lab/test_drift.py",
            "test_ospf_routes_match_intent",
            ("INTENT VIOLATED: every router must have exactly the OSPF routes the intent expects",),
        )
    ]


FAULTS: dict[str, Fault] = {
    "wrong-vlan": Fault(wrong_vlan, wrong_vlan_detected_by),
    "missing-ospf-network": Fault(missing_ospf_network, missing_ospf_network_detected_by),
}
