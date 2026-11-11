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
from netcheck.transport.base import TransportError


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
    assert (report.expected, report.actual) == ("0/5 ICMP replies h1 -> h3 over L2", "5/5 replies")
    drifted = [d for d in report.drift if d.expected != d.actual]
    assert [d.where for d in drifted] == ["sw1:p3"]
    assert [d.where for d in report.drift] == ["sw1:p1", "sw1:p3"]  # both hosts' ports
    assert "VLAN 10: p1, p2, p3, p4" in report.evidence["sw1 VLAN membership"]
    assert report.evidence["h1 neighbor table"] != ["(none)"]  # h1 resolved h3: it answered
    assert report.capture_summary[0] == "h3:p1" and report.capture_files


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
    assert (report.expected, report.actual) == ("state Full", "state absent")
    assert report.evidence["diagnosis"] == ["r1 is Full with r2 via p2; only p3 is down"]
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


# ---- one "why is this route missing" diagnosis ------------------------------------


def _unreadable(*_: object) -> object:
    from netcheck.transport.base import TransportError

    raise TransportError("r1: docker exec failed")


def test_unreadable_neighbor_table_is_not_called_no_adjacency(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    monkeypatch.setattr(parts["ospf_r1"], "neighbors", _unreadable)
    with pytest.raises(IntentViolation) as exc:
        assert_route_present(lab["r1"], "10.0.30.0/24")
    (diagnosis,) = exc.value.report.evidence["diagnosis"]
    assert diagnosis == (
        "r1: could not read its OSPF neighbors (TransportError: r1: docker exec failed)"
    )
    assert "no Full adjacency" not in diagnosis
    assert exc.value.report.evidence["evidence unavailable"][0] == (
        "r1 neighbors: TransportError: r1: docker exec failed"
    )


def test_drift_report_reads_each_router_once_and_shows_its_lsdb(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    r1, r2 = parts["ospf_r1"], parts["ospf_r2"]
    r1.nbrs = [OspfNeighbor("2.2.2.2", "Full", "p2")]
    r2.nbrs = [OspfNeighbor("1.1.1.1", "Full", "p1")]
    r1.lsdb = {"10.0.30.0/24"}
    calls = {"neighbors": 0, "lsdb": 0}

    def counted(name: str, fn):  # type: ignore[no-untyped-def]
        def wrapper():  # type: ignore[no-untyped-def]
            calls[name] += 1
            return fn()

        return wrapper

    monkeypatch.setattr(r1, "neighbors", counted("neighbors", r1.neighbors))
    monkeypatch.setattr(r1, "lsdb_prefixes", counted("lsdb", r1.lsdb_prefixes))
    # r1 is missing two routes: reads happen once all the same
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_routes_match_intent({"r1": ["10.0.30.0/24", "10.0.99.0/24"], "r2": []})
    report = exc.value.report
    assert calls == {"neighbors": 1, "lsdb": 1}
    assert report.evidence["r1 LSDB"] == ["10.0.30.0/24 present", "10.0.99.0/24 absent"]
    assert report.evidence["r1 neighbors"] == ["2.2.2.2 Full via p2"]


def test_neighbor_check_reads_the_table_once(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from netcheck.timing import WaitTimeout

    lab, parts = fake
    r1 = parts["ospf_r1"]
    r1.nbrs = [OspfNeighbor("2.2.2.2", "Full", "p2")]
    calls = {"n": 0}
    real = r1.neighbors

    def counted():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return real()

    def timed_out(check, probe, ok, **_):  # type: ignore[no-untyped-def]
        raise WaitTimeout(check, 0.0, "absent")  # skip polling: count only the report's reads

    monkeypatch.setattr(r1, "neighbors", counted)
    monkeypatch.setattr(lab.timing, "wait_until", timed_out)
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_neighbor_full(lab["r1"], lab["r2"], port="p3")
    assert calls["n"] == 1  # evidence and the "only p3 is down" diagnosis share one read
    report = exc.value.report
    assert report.evidence["r1 neighbors"] == ["2.2.2.2 Full via p2"]
    assert "only p3 is down" in report.evidence["diagnosis"][0]


# ---- route reports: what they say and when the router cannot be read (improvement 6) --

EXTRA = Route("10.9.9.0/24", "ospf", ("10.0.12.1",), True)


def _full_both_ways(parts) -> None:  # type: ignore[no-untyped-def]
    parts["ospf_r1"].nbrs = [OspfNeighbor("2.2.2.2", "Full", "p2")]
    parts["ospf_r2"].nbrs = [OspfNeighbor("1.1.1.1", "Full", "p1")]


def test_an_unexpected_route_is_drift_and_named(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    _full_both_ways(parts)
    parts["ospf_r1"].rib = R1_RIB | {EXTRA.prefix: EXTRA}
    parts["ospf_r2"].rib = R2_RIB
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_routes_match_intent()
    report = exc.value.report
    assert (report.expected, report.actual) == (
        "2 routes installed, no others",
        "0 missing, 1 unexpected",
    )
    assert [(d.where, d.expected, d.actual, d.note) for d in report.state_diff] == [
        ("r1 routing table", "absent", "10.9.9.0/24", "not in the intent")
    ]
    assert report.evidence["diagnosis"] == [
        "unexpected routes: a router advertises a prefix the intent does not list "
        "(check its OSPF interfaces)"
    ]


def test_a_missing_route_names_its_originator(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    _full_both_ways(parts)
    parts["ospf_r2"].rib = R2_RIB
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_routes_match_intent()
    assert [(d.where, d.expected, d.actual, d.note) for d in exc.value.report.state_diff] == [
        ("r1 routing table", "10.0.30.0/24", "absent", "advertised by r2")
    ]


def test_no_adjacency_with_intended_settings_points_at_link_state(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    for r in ("r1", "r2"):
        parts[f"ospf_{r}"].interfaces = lab.expected.ospf_interfaces(r)
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_routes_match_intent()
    report = exc.value.report
    assert report.drift == []
    assert report.evidence["diagnosis"] == [
        "no OSPF adjacency between the routers on any link: they exchange no routes",
        "OSPF link settings match the intent: check link state (make smoke)",
    ]


def _routes_unreadable(*_: object) -> dict[str, Route]:
    raise TransportError("r1: docker exec failed")


def test_routes_that_cannot_be_read_say_so(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    parts["ospf_r2"].rib = R2_RIB
    monkeypatch.setattr(parts["ospf_r1"], "routes", _routes_unreadable)
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_routes_match_intent()
    report = exc.value.report
    assert report.actual == "0 missing, 0 unexpected"
    assert report.evidence["last read failed"] == ["r1: docker exec failed"]


def test_a_route_known_but_not_installed_is_not_present(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions import assert_route_present

    lab, parts = fake
    parts["ospf_r1"].rib = {"10.0.30.0/24": Route("10.0.30.0/24", "ospf", ("10.0.12.1",), False)}
    with pytest.raises(IntentViolation) as exc:
        assert_route_present(lab["r1"], "10.0.30.0/24")
    report = exc.value.report
    assert (report.verdict, report.expected, report.actual) == (
        "r1 must learn 10.0.30.0/24 via OSPF from r2",
        "10.0.30.0/24 installed in r1 routing table",
        "route absent",
    )


@pytest.mark.parametrize("check", ["uses", "next_hops"])
def test_a_route_check_on_an_unreadable_router_reports_why(fake, monkeypatch, check: str) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions import assert_route_next_hops, assert_route_uses
    from netcheck.expected import NextHop

    lab, parts = fake
    monkeypatch.setattr(parts["ospf_r1"], "routes", _routes_unreadable)
    hop = NextHop("r2", "p2", "10.0.12.1")
    run = {
        "uses": lambda: assert_route_uses(lab["r1"], "10.0.30.0/24", "10.0.12.1"),
        "next_hops": lambda: assert_route_next_hops(lab["r1"], "10.0.30.0/24", [hop]),
    }[check]
    with pytest.raises(IntentViolation) as exc:
        run()
    report = exc.value.report
    assert report.verdict.startswith("r1 must r")  # no prefix unless the caller gives one
    assert report.actual == "no answer: r1: docker exec failed"
    assert (
        report.expected
        == {"uses": "next hop 10.0.12.1", "next_hops": "next hops ['10.0.12.1']"}[check]
    )
    assert report.evidence["r1 neighbors"] == ["(none)"]


def test_a_route_on_the_wrong_next_hop_shows_the_one_it_has(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions import assert_route_next_hops, assert_route_uses
    from netcheck.expected import NextHop

    lab, parts = fake
    parts["ospf_r1"].rib = {"10.0.30.0/24": Route("10.0.30.0/24", "ospf", ("10.0.12.3",), True)}
    with pytest.raises(IntentViolation) as exc:
        assert_route_uses(lab["r1"], "10.0.30.0/24", "10.0.12.1", why="before the failure, ")
    assert (exc.value.report.verdict, exc.value.report.actual) == (
        "before the failure, r1 must route 10.0.30.0/24 via 10.0.12.1",
        "next hops ['10.0.12.3']",
    )
    with pytest.raises(IntentViolation) as exc:
        assert_route_next_hops(lab["r1"], "10.0.30.0/24", [NextHop("r2", "p2", "10.0.12.1")])
    report = exc.value.report
    assert report.verdict == (
        "r1 must reach 10.0.30.0/24 via r2 (10.0.12.1), the cheapest path in the intent"
    )
    assert report.actual == "next hops ['10.0.12.3']"
    assert "r1 OSPF link settings" not in report.evidence  # read fine: drift, not a note


def test_an_unreadable_lsdb_is_noted(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    parts["ospf_r1"].nbrs = [OspfNeighbor("2.2.2.2", "Full", "p2")]
    monkeypatch.setattr(parts["ospf_r1"], "lsdb_prefixes", _routes_unreadable)
    with pytest.raises(IntentViolation) as exc:
        assert_route_present(lab["r1"], "10.0.30.0/24")
    assert exc.value.report.evidence["evidence unavailable"] == [
        "r1 LSDB: TransportError: r1: docker exec failed"
    ]


def test_every_ospf_interface_field_is_named_when_it_differs(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions.ospf import ospf_interface_drift

    lab, _ = fake
    want = lab.expected.ospf_interfaces("r1")["p2"]
    live = replace(
        want, area="1", passive=True, dead_interval=40, network="broadcast", cost=None, addresses=()
    )
    assert [(d.where, d.expected, d.actual) for d in ospf_interface_drift("r1:p2", want, live)] == [
        ("r1:p2 OSPF area", "0", "1"),
        ("r1:p2 addresses", "10.0.12.0/31", "none"),
        ("r1:p2 passive", "False", "True"),
        ("r1:p2 dead interval", "4", "40"),
        ("r1:p2 network type", "point-to-point", "broadcast"),
        ("r1:p2 cost", "10", "not set"),
    ]
    two = replace(want, addresses=("10.0.12.0/31", "10.9.0.1/24"))
    (line,) = ospf_interface_drift("r1:p2", want, two)
    assert line.actual == "10.0.12.0/31, 10.9.0.1/24"
    assert [(d.where, d.expected, d.actual) for d in ospf_interface_drift("r1:p2", want, None)] == [
        ("r1:p2 OSPF interface", "configured", "absent")
    ]


def test_an_ospf_interface_off_the_intent_is_reported(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions import assert_ospf_interface_matches_intent

    lab, parts = fake
    parts["ospf_r1"].interfaces = lab.expected.ospf_interfaces("r1")
    parts["ospf_r1"].interfaces["p2"] = replace(parts["ospf_r1"].interfaces["p2"], cost=50)
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_interface_matches_intent(lab["r1"], "p2")
    report = exc.value.report
    assert (report.verdict, report.expected, report.actual) == (
        "r1:p2 OSPF settings must match the intent",
        "OSPF area, timers, network type, cost and addresses as in the intent",
        "1 setting(s) differ",
    )
    assert report.evidence["r1:p2 live OSPF settings"] == [str(parts["ospf_r1"].interfaces["p2"])]


def test_drift_from_every_router_stays_in_the_report(fake) -> None:  # type: ignore[no-untyped-def]
    """Both routers miss a prefix, each because the other does not advertise it: the
    report keeps both routers' drift, not only the last one read."""
    lab, parts = fake
    _full_both_ways(parts)
    for r, port in (("r1", "p1.10"), ("r2", "p3")):
        parts[f"ospf_{r}"].interfaces = dict(lab.expected.ospf_interfaces(r))
        parts[f"ospf_{r}"].interfaces[port] = replace(
            parts[f"ospf_{r}"].interfaces[port], area=None
        )
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_routes_match_intent()
    report = exc.value.report
    assert [d.where for d in report.drift] == [
        "r2:p3 (eth3) OSPF area",
        "r1:p1.10 (eth1.10) OSPF area",
    ]
    assert report.evidence["diagnosis"] == [
        "r1: adjacency Full, but r2 is not advertising 10.0.30.0/24",
        "r2: adjacency Full, but r1 is not advertising 10.0.10.0/24",
    ]


def test_no_adjacency_keeps_the_link_drift_of_every_router(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    for r in ("r1", "r2"):
        parts[f"ospf_{r}"].interfaces = {
            name: replace(i, dead_interval=40) if not i.passive else i
            for name, i in lab.expected.ospf_interfaces(r).items()
        }
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_routes_match_intent()
    assert [d.where for d in exc.value.report.drift] == [
        "r1:p2 (primary) dead interval",
        "r1:p3 (backup) dead interval",
        "r2:p1 (primary) dead interval",
        "r2:p2 (backup) dead interval",
    ]


def test_a_gateway_sub_interface_off_the_intent_is_named_by_its_port(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions import assert_ospf_interface_matches_intent

    lab, parts = fake
    parts["ospf_r1"].interfaces = dict(lab.expected.ospf_interfaces("r1"))
    parts["ospf_r1"].interfaces["p1.10"] = replace(parts["ospf_r1"].interfaces["p1.10"], area="1")
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_interface_matches_intent(lab["r1"], "p1.10")
    assert [d.where for d in exc.value.report.drift] == ["r1:p1.10 OSPF area"]  # trunk: no label
