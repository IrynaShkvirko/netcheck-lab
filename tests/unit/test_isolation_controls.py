"""The isolation check's controls: zero replies counts as isolation only when the sender's
probes left it and its own path works, and no router joins the two VLANs either."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from netcheck.assertions import assert_isolated, control_target
from netcheck.expected import Expected
from netcheck.intent import load_intent
from netcheck.lab import Lab, set_current
from netcheck.report import IntentViolation

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
    lab, _ = fake
    lab["h1"].shell.world.leaks.add(frozenset({"h1", "h3"}))  # type: ignore[attr-defined]
    with pytest.raises(IntentViolation) as exc:
        assert_isolated(lab["h1"], lab["h3"])
    report = exc.value.report
    assert report.verdict == "h1 and h3 must be isolated, also through a router"
    assert report.actual == "5/5 replies"
    assert report.evidence["r1 interfaces in 10.0.20.0/24"] == ["(none)"]


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
    from netcheck.capabilities import PortVlans

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
    from netcheck.capabilities import PortVlans

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
