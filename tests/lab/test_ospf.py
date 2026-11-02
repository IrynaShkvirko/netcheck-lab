"""Functional: OSPF adjacencies and learned routes, parametrized from the intent."""

from collections.abc import Callable

import pytest

from netcheck.assertions import assert_ospf_neighbor_full, assert_route_present
from netcheck.device import Device

pytestmark = pytest.mark.functional


def test_ospf_adjacency_is_full(
    ospf_adjacency: tuple[str, str, str, str], node: Callable[[str], Device]
) -> None:
    router, neighbor, port, _link = ospf_adjacency
    assert_ospf_neighbor_full(node(router), node(neighbor), port=port)


def test_ospf_route_is_learned(ospf_route: tuple[str, str], node: Callable[[str], Device]) -> None:
    router, prefix = ospf_route
    assert_route_present(node(router), prefix)
