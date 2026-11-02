"""Drift: live state compared with the expected state generated from the intent."""

import pytest

from netcheck.assertions import assert_vlan_membership
from netcheck.lab import Lab

pytestmark = pytest.mark.drift


def test_vlan_membership_matches_intent(lab: Lab, switch_name: str) -> None:
    assert_vlan_membership(lab[switch_name])


def test_ospf_routes_match_intent(lab: Lab) -> None:
    missing = {}
    for router, prefixes in lab.expected["routes"].items():
        live = set(lab[router].ospf.routes())
        gone = sorted(set(prefixes) - live)
        if gone:
            missing[router] = gone
    assert not missing, f"routes in intent but not installed: {missing}"
