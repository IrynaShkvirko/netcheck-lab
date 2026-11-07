"""Functional: OSPF adjacencies and learned routes, parametrized from the intent."""

import pytest

from netcheck.assertions import (
    assert_ospf_interface_matches_intent,
    assert_ospf_neighbor_full,
    assert_route_present,
    check_next_hop,
)
from netcheck.expected import Adjacency
from netcheck.lab import Lab
from netcheck.steps import step

pytestmark = pytest.mark.functional


def test_ospf_adjacency_is_full(lab: Lab, ospf_adjacency: Adjacency) -> None:
    """One OSPF adjacency from the intent reaches state Full, on its own link."""
    adj = ospf_adjacency
    router_name, neighbor_name, port = adj.router, adj.neighbor, adj.local_port
    router, neighbor = lab[router_name], lab[neighbor_name]

    with step(f"Look up the {adj.link} link {router_name}:{port} -> {neighbor_name} in the intent"):
        neighbor_id = adj.router_id
        link = lab.intent.link_at(router_name, port)
        assert neighbor_id, f"{neighbor_name} has no router_id in the intent"
        assert link.peer(router_name).node == neighbor_name, (
            f"{router_name}:{port} is not on that link"
        )

    with step(f"Check {router_name}:{port} has the intended OSPF area and timers"):
        assert_ospf_interface_matches_intent(router, port)

    with step(f"Wait until {router_name} sees {neighbor_name} ({neighbor_id}) as Full via {port}"):
        assert_ospf_neighbor_full(router, neighbor, port=port)


def test_ospf_route_is_learned(lab: Lab, ospf_route: tuple[str, str]) -> None:
    """A router learns a prefix another router advertises, through that router."""
    router_name, prefix = ospf_route
    router = lab[router_name]

    with step(f"Find which router advertises {prefix} in the intent"):
        origin_name = lab.expected.originator(prefix, exclude=router_name)
        assert origin_name in lab.devices, f"no router in the intent advertises {prefix}"
        allowed_next_hops = lab.expected.next_hops(router_name, origin_name)

    with step(f"Check {router_name} has a Full OSPF adjacency with {origin_name}"):
        assert_ospf_neighbor_full(router, lab[origin_name])

    with step(f"Wait until {prefix} is installed in {router_name}'s routing table"):
        route = assert_route_present(router, prefix)

    with step(
        f"Check the route goes through {origin_name} ({', '.join(sorted(allowed_next_hops))})"
    ):
        check_next_hop(router, route, origin_name, allowed_next_hops)
