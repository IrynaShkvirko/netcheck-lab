"""The isolation check's controls: zero replies counts as isolation only when the sender's
probes left it and its own path works, and no router joins the two VLANs either."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from netcheck.assertions import assert_isolated, control_target
from netcheck.model.expected import Expected
from netcheck.model.intent import load_intent
from netcheck.run.lab import Lab, set_current
from netcheck.run.report import IntentViolation
from netcheck.run.timing import Threshold

from .fakes import build_fake_lab
from .intents import ROOT
from .test_review_fixes import _failed_tests, _run_lab_suite

TWO_SITE = ROOT / "tests" / "unit" / "intents" / "two-site.yaml"


@pytest.fixture
def two_site(tmp_path: Path) -> Generator[tuple[Lab, dict[str, Any]], None, None]:
    lab, parts = build_fake_lab(load_intent(TWO_SITE), tmp_path)
    token = set_current(lab)
    yield lab, parts
    token.var.reset(token)


# ---- what the intent says shares a VLAN ---------------------------------------------


def test_broadcast_domain_follows_trunks_between_switches() -> None:
    campus = Expected(load_intent(ROOT / "intent" / "lab.yaml"))
    assert campus.broadcast_domain("h1") == ["h2"]
    assert campus.broadcast_domain("h3") == []  # alone in VLAN 20
    assert campus.broadcast_domain("h4") == []  # on a router, not a switch
    two_site = Expected(load_intent(TWO_SITE))
    assert two_site.broadcast_domain("h1") == ["h3"]  # sw1 and sw2 share VLAN 10
    assert two_site.broadcast_domain("h4") == []  # VLAN 30 is not on the trunk


def test_vlan_gateway() -> None:
    two_site = Expected(load_intent(TWO_SITE))
    assert two_site.vlan_gateway("h1") == ("r1", "10.0.10.1")
    assert two_site.vlan_gateway("h4") == ("r3", "10.0.30.1")
    assert two_site.vlan_gateway("h2") is None  # VLAN 20 has no gateway


def test_control_target_prefers_what_depends_on_least(fake, two_site) -> None:  # type: ignore[no-untyped-def]
    campus_lab, _ = fake
    lab, _ = two_site
    set_current(campus_lab)
    target = control_target(campus_lab["h1"])
    assert (target.name, target.ip) == ("h2", "10.0.10.12")  # same switch
    set_current(lab)
    target = control_target(lab["h1"])
    assert (target.name, target.ip) == ("r1 (VLAN 10 gateway)", "10.0.10.1")  # not h3 on sw2
    assert control_target(lab["h2"]) is None  # alone in VLAN 20, no gateway


# ---- each control catches what it is for --------------------------------------------


def test_a_router_joining_the_vlans_breaks_isolation(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.model.capabilities import OspfInterface

    lab, parts = fake
    lab["h1"].shell.world.leaks.add(frozenset({"h1", "h3"}))  # type: ignore[attr-defined]
    parts["ospf_r1"].interfaces = dict(lab.expected.ospf_interfaces("r1"))
    parts["ospf_r1"].interfaces["p1.20"] = OspfInterface("p1.20", ("10.0.20.1/24",), "0")
    with pytest.raises(IntentViolation) as exc:
        assert_isolated(lab["h1"], lab["h3"])
    report = exc.value.report
    assert report.verdict == "h1 and h3 must be isolated, also through a router"
    assert report.actual == "5/5 replies"
    assert report.expected == "0/5 ICMP replies h1 -> h3 over h1's routes"
    assert report.evidence["r1 interfaces in 10.0.20.0/24"] == ["p1.20 10.0.20.1/24"]
    assert report.evidence["r2 interfaces in 10.0.20.0/24"] == ["(none)"]


def test_probes_that_never_left_the_sender_are_inconclusive(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    lab["h1"].shell.arp_silent = True  # type: ignore[attr-defined]
    with pytest.raises(IntentViolation) as exc:
        assert_isolated(lab["h1"], lab["h3"])
    report = exc.value.report
    assert report.verdict == (
        "isolation h1-h3 inconclusive: h1 never sent an ARP request for h3 (10.0.20.13)"
    )
    assert report.actual == "no entry: no probe reached the wire"


def test_a_sender_cut_off_from_its_own_vlan_is_inconclusive(fake) -> None:  # type: ignore[no-untyped-def]
    """h1's port carries no VLAN 10 any more: h3 stays silent, but so would anything.
    Before the control, this passed as isolated."""
    from netcheck.model.capabilities import PortVlans

    lab, parts = fake
    parts["bridge"].ports["p1"] = PortVlans("access", (99,))
    with pytest.raises(IntentViolation) as exc:
        assert_isolated(lab["h1"], lab["h3"])
    report = exc.value.report
    assert report.verdict == (
        "isolation h1-h3 inconclusive: h1 cannot reach h2 (10.0.10.12), in its own VLAN, "
        "over layer 2"
    )
    assert report.actual == "0/3 replies"
    drifted = [(d.where, d.expected, d.actual) for d in report.drift if d.expected != d.actual]
    assert drifted == [("sw1:p1", "access VLAN 10", "access VLAN 99")]


def test_a_reply_is_a_violation_even_when_the_control_would_fail(fake) -> None:  # type: ignore[no-untyped-def]
    """h3 moved into VLAN 10 answers h1, and h1's control target h2 is broken too: the
    report is the violation, not 'inconclusive'. The control only validates silence."""
    from netcheck.model.capabilities import PortVlans

    lab, parts = fake
    parts["bridge"].ports["p3"] = PortVlans("access", (10,))  # the wrong-vlan fault
    parts["bridge"].ports["p2"] = PortVlans("access", (99,))  # h2, h1's control, cut off
    with pytest.raises(IntentViolation) as exc:
        assert_isolated(lab["h1"], lab["h3"])
    assert exc.value.report.verdict == "h1 (VLAN 10) and h3 (VLAN 20) must be isolated"


def test_isolation_passes_with_every_control_on_a_healthy_lab(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    result = assert_isolated(lab["h1"], lab["h3"])
    assert (result.sent, result.received) == (5, 0)
    assert "10.0.10.11/32" in lab["h3"].shell.routes  # type: ignore[attr-defined]  # h3 ARPs for h1


# ---- the real lab test ----------------------------------------------------------------


def test_lab_isolation_test_fails_when_the_sender_port_is_broken(pytester) -> None:  # type: ignore[no-untyped-def]
    """The real tests/lab file: with h1's port out of VLAN 10, both isolation cases fail as
    inconclusive at their last step, naming sw1:p1, instead of passing."""
    result = _run_lab_suite(pytester, False, "-k", "isolated", bad_ports={"p1": 99})
    assert _failed_tests(result) == {
        "test_hosts_in_different_vlans_are_isolated[h1-h3]",
        "test_hosts_in_different_vlans_are_isolated[h2-h3]",
    }
    result.stdout.fnmatch_lines(
        [
            "*INTENT VIOLATED: isolation h1-h3 inconclusive: h1 cannot reach h2 (10.0.10.12)*",
            "*8. Check h1 reaches h2 (10.0.10.12), in its own VLAN, over layer 2 [FAILED*",
            "*sw1:p1: expected 'access VLAN 10', actual 'access VLAN 99'*",
        ]
    )


# ---- what the controls send and report (mutation floor, improvement 6) ---------------


def test_a_control_target_on_another_switch_when_the_vlan_has_no_gateway(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import yaml

    from netcheck.assertions.l2 import ControlTarget

    raw = yaml.safe_load(TWO_SITE.read_text())
    del raw["vlans"][10]["gateway"]
    for h in ("h1", "h3"):
        raw["nodes"][h].pop("gateway", None)
    path = tmp_path / "no-gateway.yaml"
    path.write_text(yaml.safe_dump(raw))
    lab, _ = build_fake_lab(load_intent(path), tmp_path)
    token = set_current(lab)
    try:
        assert control_target(lab["h1"]) == ControlTarget("h3", "10.0.10.13", "h3")
    finally:
        token.var.reset(token)


def test_a_same_switch_control_target_is_a_host_to_force_back(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions.l2 import ControlTarget

    lab, _ = fake
    assert control_target(lab["h1"]) == ControlTarget("h2", "10.0.10.12", "h2")


def test_the_control_pings_its_target_over_forced_routes_both_ways(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions.l2 import check_layer2_control
    from netcheck.model.capabilities import PingResult

    lab, parts = fake
    calls: list[tuple[str, dict[str, float]]] = []

    def ping(dst: str, **kw: float) -> PingResult:
        calls.append((dst, kw))
        return PingResult("h1", dst, 3, 1)  # one reply proves the path

    monkeypatch.setattr(lab["h1"].shell, "ping", ping)
    check_layer2_control(lab["h1"], lab["h3"], control_target(lab["h1"]))
    assert calls == [("10.0.10.12", {"count": 3, "interval": 0.2, "deadline": 3})]
    assert parts["shell_h1"].routes == ["10.0.10.12/32"]  # h1 must ARP for h2
    assert parts["shell_h2"].routes == ["10.0.10.11/32"]  # and h2 for h1
    assert lab.cleanup.run() == []
    assert parts["shell_h1"].routes == parts["shell_h2"].routes == []


def test_a_failed_control_reports_both_ports_the_link_and_the_neighbors(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.model.capabilities import PortVlans

    lab, parts = fake
    lab.timing.thresholds["reachability"] = Threshold("reachability", timeout=0.01, poll=0.001)
    parts["bridge"].ports["p1"] = PortVlans("access", (99,))
    with pytest.raises(IntentViolation) as exc:
        assert_isolated(lab["h1"], lab["h3"])
    report = exc.value.report
    assert report.expected == "replies from 10.0.10.12: proof that h1's path works"
    assert [d.where for d in report.drift] == ["sw1:p1", "sw1:p2"]  # h1's port and h2's
    assert report.evidence["sw1 VLAN membership"] == [
        "VLAN 10: p2, p4",
        "VLAN 20: p3, p4",
        "VLAN 99: p1",
    ]
    assert report.evidence["h1 link"] == [lab["h1"].links.link_state("p1").line()]
    assert "h1 neighbor table" in report.evidence


def test_no_control_target_is_a_warning(fake, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions.l2 import check_layer2_control

    lab, _ = fake
    with pytest.warns(UserWarning, match="no host or gateway") as caught:
        check_layer2_control(lab["h3"], lab["h1"], None)
    [w] = caught
    assert str(w.message) == (
        "isolation h3-h1: no host or gateway shares h3's VLAN, so the path the probe takes "
        "cannot be proven"
    )


def test_describe_and_diff_vlans() -> None:
    from netcheck.assertions.l2 import describe_vlans, vlan_diff
    from netcheck.model.capabilities import PortVlans

    assert describe_vlans("access", (10,)) == "access VLAN 10"
    assert describe_vlans("trunk", (10, 20)) == "trunk VLAN 10,20"
    assert describe_vlans("none", (10,)) == "none, no VLAN"
    assert describe_vlans("trunk", ()) == "trunk, no VLAN"
    exp = {"p1": PortVlans("access", (10,)), "p2": PortVlans("access", (20,))}
    live = {"p1": PortVlans("access", (10,)), "p9": PortVlans("access", (10,))}
    lines = vlan_diff("sw1", exp, live, ["p1", "p2", "p9"])
    assert [(d.where, d.expected, d.actual, d.note) for d in lines] == [
        ("sw1:p1", "access VLAN 10", "access VLAN 10", ""),
        ("sw1:p2", "access VLAN 20", "absent", "not on the bridge"),
        ("sw1:p9", "absent", "access VLAN 10", "not in the intent"),
    ]


def test_assert_vlan_membership_reads_the_switch_against_the_intent(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions import assert_vlan_membership
    from netcheck.model.capabilities import PortVlans

    lab, parts = fake
    assert_vlan_membership(lab["sw1"])
    parts["bridge"].ports["p3"] = PortVlans("access", (10,))
    with pytest.raises(IntentViolation) as exc:
        assert_vlan_membership(lab["sw1"])
    report = exc.value.report
    assert (report.verdict, report.expected, report.actual) == (
        "sw1 VLAN membership must match the intent",
        "all ports as in intent",
        "1 port(s) differ",
    )
    assert report.evidence["sw1 VLAN membership"] == ["VLAN 10: p1, p2, p3, p4", "VLAN 20: p4"]
