"""Assertions collect discriminating evidence; reports carry all eight parts (ADR-0005)."""

import pytest

from netcheck.assertions import assert_isolated, assert_route_present, interface_blocks
from netcheck.capabilities import OspfNeighbor, PortVlans, Route
from netcheck.report import FailureReport, IntentViolation


def test_isolation_passes_and_cleans_up(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    result = assert_isolated(lab["h1"], lab["h3"])
    assert result.received == 0
    assert len(lab.cleanup) >= 2  # on-link routes registered for undo
    lab.cleanup.run()
    assert lab["h1"].shell.routes == []  # type: ignore[attr-defined]


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
    r2.config = "interface eth3\n ip address 10.0.30.1/24\n!\ninterface eth1\n ip ospf area 0\n!\n"
    with pytest.raises(IntentViolation) as exc:
        assert_route_present(lab["r1"], "10.0.30.0/24")
    report = exc.value.report
    assert report.verdict == "r1 must learn 10.0.30.0/24 via OSPF from r2"
    assert any("not advertising" in ln for ln in report.evidence["diagnosis"])
    (drift,) = report.drift
    assert drift.where == "r2:p3 (eth3)" and drift.actual == "not in OSPF"


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


def test_interface_blocks_parse_frr_config() -> None:
    cfg = (
        "interface eth2\n ip ospf area 0\n ip ospf cost 10\n!\n"
        "router ospf\n ospf router-id 1.1.1.1\n"
    )
    assert interface_blocks(cfg) == {"eth2": ["ip ospf area 0", "ip ospf cost 10"]}
