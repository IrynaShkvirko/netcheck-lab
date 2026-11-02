"""Functional: VLAN isolation and reachability, parametrized from the intent."""

from collections.abc import Callable

import pytest

from netcheck.assertions import assert_isolated, assert_reachable
from netcheck.device import Device

pytestmark = pytest.mark.functional


def test_hosts_in_different_vlans_are_isolated(
    isolated_pair: list[str], node: Callable[[str], Device]
) -> None:
    a, b = isolated_pair
    assert_isolated(node(a), node(b))


def test_hosts_reach_each_other(reachable_pair: list[str], node: Callable[[str], Device]) -> None:
    a, b = reachable_pair
    assert_reachable(node(a), node(b))
