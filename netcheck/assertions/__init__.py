"""Domain assertions (ADR-0002, ADR-0005).

Each assertion checks one intent and, when it fails, collects the evidence that
explains why before raising IntentViolation. Tests read like requirements:

    assert_isolated(h1, h3)
    assert_ospf_neighbor_full(r1, r2, port="p3")

Assertions only use capabilities and normalized types; they never parse NOS output.
"""

from netcheck.assertions.common import start_capture
from netcheck.assertions.l2 import (
    assert_isolated,
    assert_vlan_membership,
    check_no_extra_ports,
    check_no_replies,
    check_port_vlans,
    check_vlan_membership,
    describe_vlans,
    force_layer2_path,
    isolation_probe_count,
    send_isolation_probes,
)
from netcheck.assertions.ospf import (
    assert_ospf_interface_matches_intent,
    assert_ospf_neighbor_full,
    assert_ospf_routes_match_intent,
    assert_route_present,
    assert_route_uses,
    check_next_hop,
)
from netcheck.assertions.reachability import assert_reachable

__all__ = [
    "assert_isolated",
    "assert_ospf_interface_matches_intent",
    "assert_ospf_neighbor_full",
    "assert_ospf_routes_match_intent",
    "assert_reachable",
    "assert_route_present",
    "assert_route_uses",
    "assert_vlan_membership",
    "check_next_hop",
    "check_no_extra_ports",
    "check_no_replies",
    "check_port_vlans",
    "check_vlan_membership",
    "describe_vlans",
    "force_layer2_path",
    "isolation_probe_count",
    "send_isolation_probes",
    "start_capture",
]
