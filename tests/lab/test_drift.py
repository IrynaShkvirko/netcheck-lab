"""Drift: live state compared with the expected state generated from the intent."""

import pytest

from netcheck.assertions import assert_ospf_routes_match_intent, check_vlan_membership
from netcheck.lab import Lab
from netcheck.steps import step

pytestmark = pytest.mark.drift


def test_vlan_membership_matches_intent(lab: Lab, switch_name: str) -> None:
    """Every switch port carries exactly the VLANs the intent gives it."""
    switch = lab[switch_name]

    with step(f"Read from the intent which VLANs each {switch_name} port must carry"):
        expected = lab.expected["vlan_membership"][switch_name]
        # e.g. {'p1': {'mode': 'access', 'vlans': [10]}, 'p4': {'mode': 'trunk', 'vlans': [10, 20]}}

    with step(f"Read the live VLAN membership from {switch_name}"):
        live = switch.bridge.vlan_membership()

    with step(f"Compare each of the {len(expected)} ports: mode and VLANs"):
        check_vlan_membership(switch_name, expected, live)


def test_ospf_routes_match_intent(lab: Lab) -> None:
    """Every router has exactly the OSPF routes the intent implies: none missing, none extra."""
    with step("Work out from the intent which routes each router must learn"):
        expected = lab.expected["routes"]
        # e.g. {'r1': ['10.0.30.0/24'], 'r2': ['10.0.10.0/24']}: each learns the other's LANs

    total = sum(len(prefixes) for prefixes in expected.values())
    with step(f"Wait until the routers have exactly these {total} OSPF routes installed"):
        assert_ospf_routes_match_intent(expected)
