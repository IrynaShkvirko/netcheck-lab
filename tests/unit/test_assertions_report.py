"""Assertions collect discriminating evidence; reports carry all eight parts (ADR-0005)."""

from dataclasses import replace

import pytest

from netcheck.assertions import (
    assert_isolated,
    assert_ospf_neighbor_full,
    assert_ospf_routes_match_intent,
    assert_route_present,
    check_vlan_membership,
)
from netcheck.capabilities import OspfNeighbor, PortVlans, Route
from netcheck.ports import PortMap
from netcheck.report import FailureReport, IntentViolation


def test_isolation_passes_and_cleans_up(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    result = assert_isolated(lab["h1"], lab["h3"])
    assert result.received == 0
    assert len(lab.cleanup) >= 2  # on-link routes registered for undo
    lab.cleanup.run()
    assert lab["h1"].shell.routes == []  # type: ignore[attr-defined]


def test_vlan_membership_rejects_a_port_the_intent_omits() -> None:
    """A live port with no intent entry is drift, the same way an extra OSPF route is."""
    expected = {"p1": PortVlans("access", (10,))}
    live = {"p1": PortVlans("access", (10,)), "p9": PortVlans("access", (10,))}
    with pytest.raises(IntentViolation) as exc:
        check_vlan_membership("sw1", expected, live)
    (drift,) = exc.value.report.drift
    assert (drift.where, drift.expected, drift.actual, drift.note) == (
        "sw1:p9",
        "absent",
        "access VLAN 10",
        "not in the intent",
    )


def test_wrong_vlan_report_points_at_the_drifted_port(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    parts["bridge"].ports["p3"] = PortVlans("access", (10,))  # the injected fault
    with pytest.raises(IntentViolation) as exc:
        assert_isolated(lab["h1"], lab["h3"])
    report = exc.value.report
    assert report.verdict == "h1 (VLAN 10) and h3 (VLAN 20) must be isolated"
    drifted = [d for d in report.drift if d.expected != d.actual]
    assert [d.where for d in drifted] == ["sw1:p3"]
    assert "VLAN 10: p1, p2, p3, p4" in report.evidence["sw1 VLAN membership"]
    assert report.capture_summary and report.capture_files


def test_missing_ospf_network_is_diagnosed_as_not_advertised(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    r1, r2 = parts["ospf_r1"], parts["ospf_r2"]
    r1.nbrs = [OspfNeighbor("2.2.2.2", "Full", "p2"), OspfNeighbor("2.2.2.2", "Full", "p3")]
    r1.rib = {}  # 10.0.30.0/24 missing
    r2.interfaces = lab.expected.ospf_interfaces("r2")
    r2.interfaces["p3"] = replace(r2.interfaces["p3"], area=None)  # LAN taken out of OSPF
    with pytest.raises(IntentViolation) as exc:
        assert_route_present(lab["r1"], "10.0.30.0/24")
    report = exc.value.report
    assert report.verdict == "r1 must learn 10.0.30.0/24 via OSPF from r2"
    assert any("not advertising" in ln for ln in report.evidence["diagnosis"])
    (drift,) = report.drift
    assert drift.where == "r2:p3 (eth3) OSPF area" and drift.actual == "not in OSPF"


def test_route_present_passes(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    parts["ospf_r1"].rib = {"10.0.30.0/24": Route("10.0.30.0/24", "ospf", ("10.0.12.1",), True)}
    assert assert_route_present(lab["r1"], "10.0.30.0/24").installed


def test_report_renders_all_eight_parts() -> None:
    from netcheck.report import DiffLine

    r = FailureReport(
        verdict="h1 and h3 must be isolated",
        expected="0/5",
        actual="5/5",
        drift=[DiffLine("sw1:p3", "access VLAN 20", "access VLAN 10")],
        evidence={"sw1 VLAN membership": ["VLAN 10: p1, p2, p3"]},
        commands=["[sw1] exec: bridge -j vlan show rc=0 (0.02s)"],
        capture_summary=["ICMP echo request"],
        capture_files=["a.pcap"],
        environment={"backend": "local", "intent": "abc"},
        repro_command="pytest x --snapshot s1",
        snapshot_id="s1",
    )
    text = r.render()
    for part in (
        "INTENT VIOLATED",
        "Expected:",
        "<-- drift",
        "Evidence:",
        "Capture",
        "Commands run",
        "Env:",
        "Repro:",
        "restore s1",
    ):
        assert part in text


def test_parse_frr_interfaces_normalizes_settings() -> None:
    from netcheck.backends.local import parse_frr_interfaces

    cfg = (
        "interface eth1.10\n ip address 10.0.10.1/24\n ip ospf area 0\n ip ospf passive\nexit\n!\n"
        "interface eth2\n ip address 10.0.12.0/31\n ip ospf area 0.0.0.0\n ip ospf cost 10\n"
        " ip ospf dead-interval 4\n ip ospf hello-interval 1\n"
        " ip ospf network point-to-point\nexit\n!\nrouter ospf\n ospf router-id 1.1.1.1\n"
    )
    s = parse_frr_interfaces(cfg, PortMap({"p1": "eth1", "p2": "eth2"}))
    assert s["p1.10"].passive and s["p1.10"].addresses == ("10.0.10.1/24",)
    assert (s["p2"].area, s["p2"].cost, s["p2"].hello_interval, s["p2"].dead_interval) == (
        "0.0.0.0",
        10,
        1,
        4,
    )
    assert s["p2"].network == "point-to-point"


def test_adjacency_is_checked_per_link(fake) -> None:  # type: ignore[no-untyped-def]
    """Backup adjacency down, primary Full: only the backup case may fail."""
    lab, parts = fake
    parts["ospf_r1"].nbrs = [OspfNeighbor("2.2.2.2", "Full", "p2")]  # p3 (backup) is gone
    assert_ospf_neighbor_full(lab["r1"], lab["r2"], port="p2")
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_neighbor_full(lab["r1"], lab["r2"], port="p3")
    report = exc.value.report
    assert report.verdict == "r1 must have a Full OSPF adjacency with r2 (2.2.2.2) via r1:p3"
    assert report.actual == "state absent"
    assert "only p3 is down" in report.evidence["diagnosis"][0]
    assert list(k for k in report.evidence if "link" in k) == ["r1:p3 link (backup)"]


R1_RIB = {"10.0.30.0/24": Route("10.0.30.0/24", "ospf", ("10.0.12.1",), True)}
R2_RIB = {"10.0.10.0/24": Route("10.0.10.0/24", "ospf", ("10.0.12.0",), True)}


def test_routes_match_intent_passes(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    parts["ospf_r1"].rib, parts["ospf_r2"].rib = R1_RIB, R2_RIB
    assert_ospf_routes_match_intent()


def test_routes_match_intent_waits_for_convergence(fake) -> None:  # type: ignore[no-untyped-def]
    """Right after a deploy the routes appear a moment later: that is not a failure."""
    lab, parts = fake
    parts["ospf_r2"].rib = R2_RIB
    calls = {"n": 0}

    def r1_routes() -> dict[str, Route]:
        calls["n"] += 1
        return R1_RIB if calls["n"] > 1 else {}

    parts["ospf_r1"].routes = r1_routes
    assert_ospf_routes_match_intent()
    assert calls["n"] >= 2


def test_routes_match_intent_diagnoses_no_adjacency(fake) -> None:  # type: ignore[no-untyped-def]
    """Both routers lose each other's prefix: no adjacency on any link, and the
    report points at the OSPF setting that differs from the intent."""
    lab, parts = fake
    r1, r2 = parts["ospf_r1"], parts["ospf_r2"]
    r1.interfaces = lab.expected.ospf_interfaces("r1")
    r2.interfaces = {
        name: replace(i, hello_interval=5) if not i.passive else i  # hello mismatch, both links
        for name, i in lab.expected.ospf_interfaces("r2").items()
    }
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_routes_match_intent()
    report = exc.value.report
    assert report.actual == "2 missing, 0 unexpected"
    assert [(d.where, d.expected) for d in report.state_diff] == [
        ("r1 routing table", "10.0.30.0/24"),
        ("r2 routing table", "10.0.10.0/24"),
    ]
    assert report.evidence["r1 neighbors"] == ["(none)"]
    assert "no OSPF adjacency between the routers on any link" in report.evidence["diagnosis"][0]
    assert [(d.where, d.expected, d.actual) for d in report.drift] == [
        ("r2:p1 (primary) hello interval", "1", "5"),
        ("r2:p2 (backup) hello interval", "1", "5"),
    ]


def test_routes_match_intent_diagnoses_not_advertising(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    r1, r2 = parts["ospf_r1"], parts["ospf_r2"]
    r1.nbrs = [OspfNeighbor("2.2.2.2", "Full", "p2"), OspfNeighbor("2.2.2.2", "Full", "p3")]
    r2.nbrs = [OspfNeighbor("1.1.1.1", "Full", "p1"), OspfNeighbor("1.1.1.1", "Full", "p2")]
    r2.rib = R2_RIB  # r2 is fine; r1 is missing r2's LAN
    r2.interfaces = lab.expected.ospf_interfaces("r2")
    r2.interfaces["p3"] = replace(r2.interfaces["p3"], area=None)  # no 'ip ospf area 0'
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_routes_match_intent()
    report = exc.value.report
    assert report.evidence["diagnosis"] == [
        "r1: adjacency Full, but r2 is not advertising 10.0.30.0/24"
    ]
    (drift,) = report.drift
    assert drift.where == "r2:p3 (eth3) OSPF area" and drift.actual == "not in OSPF"


# ---- one comparison rule for OSPF interfaces --------------------------------------


def test_area_written_as_dotted_quad_is_not_drift(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions.ospf import ospf_interface_drift

    lab, _ = fake
    want = lab.expected.ospf_interfaces("r2")["p3"]
    assert ospf_interface_drift("r2:p3", want, replace(want, area="0.0.0.0")) == []


def test_changed_cost_and_missing_gateway_address_are_drift(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions.ospf import ospf_interface_drift

    lab, _ = fake
    core = lab.expected.ospf_interfaces("r1")["p2"]
    (cost,) = ospf_interface_drift("r1:p2", core, replace(core, cost=100))
    assert (cost.where, cost.expected, cost.actual) == ("r1:p2 cost", "10", "100")
    gw = lab.expected.ospf_interfaces("r1")["p1.10"]
    (addr,) = ospf_interface_drift("r1:p1.10", gw, replace(gw, addresses=()))
    assert (addr.where, addr.expected, addr.actual) == (
        "r1:p1.10 addresses",
        "10.0.10.1/24",
        "none",
    )
