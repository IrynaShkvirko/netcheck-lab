"""Drift: live state compared with the expected state generated from the intent."""

import pytest

from netcheck.assertions import (
    assert_ospf_routes_match_intent,
    check_no_extra_ports,
    check_port_vlans,
    describe_vlans,
)
from netcheck.lab import Lab
from netcheck.steps import step

pytestmark = pytest.mark.drift


def test_vlan_membership_matches_intent(lab: Lab, switch_name: str) -> None:
    """Every intended switch port carries exactly its VLANs, and the switch has no
    bridge port the intent does not list.

    One soft step per port, plus one for extra ports: a failed check is recorded and the
    next one still runs, so one run lists everything that differs.
    """
    switch = lab[switch_name]

    with step(f"Read from the intent which VLANs each {switch_name} port must carry"):
        expected = lab.expected["vlan_membership"][switch_name]
        # e.g. {'p1': {'mode': 'access', 'vlans': [10]}, 'p4': {'mode': 'trunk', 'vlans': [10, 20]}}

    with step(f"Read the live VLAN membership from {switch_name}"):
        live = switch.bridge.vlan_membership()

    for port, spec in sorted(expected.items()):
        intended = describe_vlans(spec["mode"], spec["vlans"])
        with step(f"Check {switch_name}:{port} is {intended}", continue_on_failure=True):
            check_port_vlans(switch_name, port, expected, live)

    with step(
        f"Check {switch_name} has no bridge ports the intent does not list",
        continue_on_failure=True,
    ):
        check_no_extra_ports(switch_name, expected, live)


def test_ospf_routes_match_intent(lab: Lab) -> None:
    """Every router has exactly the OSPF routes the intent implies: none missing, none extra."""
    with step("Work out from the intent which routes each router must learn"):
        expected = lab.expected["routes"]
        # e.g. {'r1': ['10.0.30.0/24'], 'r2': ['10.0.10.0/24']}: each learns the other's LANs

    total = sum(len(prefixes) for prefixes in expected.values())
    with step(f"Wait until the routers have exactly these {total} OSPF routes installed"):
        assert_ospf_routes_match_intent(expected)
