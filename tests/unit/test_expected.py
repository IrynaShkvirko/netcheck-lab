"""Expected state from the campus-mini intent, query by query."""

from __future__ import annotations

import pytest

from netcheck.capabilities import OspfInterface, PortVlans
from netcheck.expected import Adjacency, Attachment, Expected, NextHop
from netcheck.intent import load_intent

from .intents import ROOT


@pytest.fixture
def expected(intent) -> Expected:  # type: ignore[no-untyped-def]
    return Expected(intent)


def test_vlan_membership_uses_the_bridge_capability_type(expected: Expected) -> None:
    sw1 = expected.vlan_membership("sw1")
    assert sw1["p3"] == PortVlans("access", (20,))
    assert sw1["p4"] == PortVlans("trunk", (10, 20))
    assert expected.switches() == ["sw1"]
    assert expected.vlan_membership("r1") == {}  # not a switch


def test_each_router_learns_the_other_routers_lans(expected: Expected) -> None:
    assert expected.routers() == ["r1", "r2"]
    assert expected.routes("r1") == {"10.0.30.0/24"}
    assert expected.routes_by_router() == {"r1": {"10.0.30.0/24"}, "r2": {"10.0.10.0/24"}}


def test_originator_and_cheapest_next_hops(expected: Expected) -> None:
    assert expected.originator("10.0.30.0/24") == "r2"
    assert expected.originator("10.0.10.0/24", exclude="r1") == "?"
    # primary (cost 10) beats backup (cost 100) in both directions
    assert expected.ospf_next_hops("r1", "10.0.30.0/24") == [NextHop("r2", "p2", "10.0.12.1")]
    assert expected.ospf_next_hops("r2", "10.0.10.0/24") == [NextHop("r1", "p1", "10.0.12.0")]
    assert expected.ospf_next_hops("r1", "10.0.10.0/24") == []  # its own VLAN gateway
    assert expected.ospf_next_hops("r1", "192.0.2.0/24") == []  # nobody originates it


def test_cheapest_path_can_go_through_a_third_router() -> None:
    """two-site: r1 and r3 share a direct link (cost 50), but the path via r2 costs 20."""
    two_site = Expected(load_intent(ROOT / "tests/unit/intents/two-site.yaml"))
    assert two_site.ospf_next_hops("r1", "10.0.30.0/24") == [NextHop("r2", "p2", "10.0.12.1")]
    assert two_site.ospf_next_hops("r3", "10.0.10.0/24") == [NextHop("r2", "p2", "10.0.23.0")]
    # a link network has two originators: the cheaper side wins (r2 via r3 is 20, via r1 60)
    assert two_site.ospf_next_hops("r3", "10.0.12.0/31") == [NextHop("r2", "p2", "10.0.23.0")]


def test_equal_cost_paths_are_all_next_hops() -> None:
    """two-site: r2 reaches the r1-r3 link network via r1 or via r3, both cost 60."""
    two_site = Expected(load_intent(ROOT / "tests/unit/intents/two-site.yaml"))
    assert two_site.ospf_next_hops("r2", "10.0.13.0/31") == [
        NextHop("r1", "p1", "10.0.12.0"),
        NextHop("r3", "p2", "10.0.23.1"),
    ]


def test_two_links_between_the_same_routers_are_two_adjacencies(expected: Expected) -> None:
    r1 = [a for a in expected.adjacencies() if a.router == "r1"]
    assert r1 == [
        Adjacency("r1", "r2", "2.2.2.2", "p2", "primary"),
        Adjacency("r1", "r2", "2.2.2.2", "p3", "backup"),
    ]
    assert expected.router_link_ports("r1") == ["p2", "p3"]
    assert [lk.label for lk in expected.links_between("r2", "r1")] == ["primary", "backup"]


def test_ospf_interfaces_are_what_a_healthy_router_reports(expected: Expected) -> None:
    r1 = expected.ospf_interfaces("r1")
    assert list(r1) == ["p1.10", "p2", "p3"]  # link order, VLAN gateway sub-interface first
    assert r1["p1.10"] == OspfInterface("p1.10", ("10.0.10.1/24",), "0", passive=True)
    assert r1["p2"] == OspfInterface(
        "p2", ("10.0.12.0/31",), "0", 1, 4, cost=10, network="point-to-point"
    )
    assert expected.ospf_interfaces("r2")["p3"].passive  # the LAN to h4
    assert expected.ospf_interfaces("sw1") == {}


def test_advertising_interfaces_include_vlan_gateways(expected: Expected) -> None:
    (lan,) = expected.advertising_interfaces("r2", "10.0.30.0/24")
    assert (lan.name, lan.addresses) == ("p3", ("10.0.30.1/24",))
    (gw,) = expected.advertising_interfaces("r1", "10.0.10.0/24")
    assert (gw.name, gw.addresses) == ("p1.10", ("10.0.10.1/24",))


def test_attachment_is_none_for_a_host_behind_a_router(expected: Expected) -> None:
    assert expected.attachment("h1") == Attachment("sw1", "p1", 10)
    assert expected.attachment("h4") is None


def test_pairs_and_failover_plan(expected: Expected) -> None:
    assert ("h1", "h3") in expected.isolated_pairs()
    plan = expected.failover()
    assert (plan.router, plan.primary_port, plan.primary_hop, plan.backup_hop) == (
        "r1",
        "p2",
        "10.0.12.1",
        "10.0.12.3",
    )
    assert (plan.prefix, plan.dst) == ("10.0.30.0/24", "h4")
