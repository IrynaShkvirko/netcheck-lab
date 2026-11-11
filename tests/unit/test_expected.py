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


# --- edges no shipped intent has (mutation floor, improvement 6) ---


def _edited(tmp_path, edit):  # type: ignore[no-untyped-def]
    from .test_intent_validation import load

    return Expected(load(tmp_path, edit))


def test_a_broadcast_domain_follows_trunks_across_several_switches(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """sw1 - sw2 - sw3 by VLAN 10 trunks: h5 on sw3 shares h1's domain two switches away."""

    def chain(raw):  # type: ignore[no-untyped-def]
        raw["nodes"].update(
            sw2={"role": "switch"},
            sw3={"role": "switch"},
            h5={"role": "host", "ip": "10.0.10.15/24"},
        )
        raw["links"] += [
            {
                "a": {"node": "sw1", "port": "p5"},
                "b": {"node": "sw2", "port": "p1"},
                "mode": "trunk",
                "vlans": [10],
            },
            {
                "a": {"node": "sw2", "port": "p2"},
                "b": {"node": "sw3", "port": "p1"},
                "mode": "trunk",
                "vlans": [10],
            },
            {
                "a": {"node": "h5", "port": "p1"},
                "b": {"node": "sw3", "port": "p2"},
                "mode": "access",
                "vlan": 10,
            },
        ]

    assert _edited(tmp_path, chain).broadcast_domain("h1") == ["h2", "h5"]


def test_vlan_gateway_for_hosts_without_one(tmp_path, expected: Expected) -> None:  # type: ignore[no-untyped-def]
    assert expected.vlan_gateway("h1") == ("r1", "10.0.10.1")
    assert expected.vlan_gateway("h3") is None  # VLAN 20 has no gateway
    assert expected.vlan_gateway("h4") is None  # on a router port, in no VLAN
    named_only = _edited(tmp_path, lambda r: r["vlans"][20].update(gateway={"node": "r1"}))
    assert named_only.vlan_gateway("h3") is None  # a router but no address


def test_failover_plan_from_the_first_reachable_pair_and_the_lan_side_with_an_address(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    def flipped(raw):  # type: ignore[no-untyped-def]
        lan = next(lk for lk in raw["links"] if lk.get("ospf_passive"))
        lan["a"], lan["b"] = lan["b"], lan["a"]  # h4 first, r2 (with the address) second
        raw["expect"]["reachable"] = [["h2", "h4"], ["h1", "h4"]]

    plan = _edited(tmp_path, flipped).failover()
    assert (plan.prefix, plan.src, plan.dst) == ("10.0.30.0/24", "h2", "h4")


def _no_labels(raw):  # type: ignore[no-untyped-def]
    for lk in raw["links"]:
        lk.pop("label", None)


def _no_passive_lan(raw):  # type: ignore[no-untyped-def]
    for lk in raw["links"]:
        lk.pop("ospf_passive", None)


def _no_backup_address(raw):  # type: ignore[no-untyped-def]
    next(lk for lk in raw["links"] if lk.get("label") == "backup")["b"].pop("ip")
    next(lk for lk in raw["links"] if lk.get("label") == "backup")["a"].pop("ip")


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (_no_labels, "intent has no links labelled 'primary' and 'backup'"),
        (_no_passive_lan, "intent has no passive LAN or no reachable pair to send traffic"),
        (
            lambda r: r["expect"].update(reachable=[]),
            "intent has no passive LAN or no reachable pair to send traffic",
        ),
        (_no_backup_address, "primary/backup links or the far LAN have no addresses"),
    ],
)
def test_failover_needs_labels_a_lan_traffic_and_addresses(tmp_path, edit, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(LookupError) as exc:
        _edited(tmp_path, edit).failover()
    assert str(exc.value) == message
