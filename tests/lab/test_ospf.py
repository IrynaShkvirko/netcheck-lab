"""Functional: OSPF adjacencies and learned routes, parametrized from the intent."""

import pytest

from netcheck.assertions import (
    assert_ospf_interface_matches_intent,
    assert_ospf_neighbor_full,
    assert_route_next_hops,
    assert_route_present,
    describe_next_hops,
)
from netcheck.model.expected import Adjacency
from netcheck.run.lab import Lab
from netcheck.run.steps import step

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
    """A router learns a prefix another router advertises, along the cheapest path the
    intent's OSPF costs give (every equal-cost path when costs tie)."""
    router_name, prefix = ospf_route
    router = lab[router_name]

    with step(f"Work out from the intent's OSPF costs which way {router_name} reaches {prefix}"):
        origin_name = lab.expected.originator(prefix, exclude=router_name)
        hops = lab.expected.ospf_next_hops(router_name, prefix)
        assert hops, f"the intent gives {router_name} no path to {prefix} (from {origin_name})"
        # e.g. r1 reaches 10.0.30.0/24, r2's LAN, via r2 over the primary link (cost 10),
        # not the backup (cost 100)

    for hop in hops:
        with step(
            f"Check {router_name} has a Full OSPF adjacency with {hop.neighbor} via {hop.port}"
        ):
            assert_ospf_neighbor_full(router, lab[hop.neighbor], port=hop.port)

    with step(f"Wait until {prefix} is installed in {router_name}'s routing table"):
        assert_route_present(router, prefix)

    with step(f"Wait until the route goes via {describe_next_hops(hops)}, the cheapest path"):
        assert_route_next_hops(router, prefix, hops)
