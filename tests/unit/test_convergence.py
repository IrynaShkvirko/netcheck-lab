"""Reconvergence measurement on the healthy fake lab (ADR-0004)."""

from __future__ import annotations

import pytest

from netcheck.convergence import measure_reconvergence
from netcheck.timing import Timing


@pytest.fixture
def no_sleep(monkeypatch):  # type: ignore[no-untyped-def]
    import netcheck.convergence as convergence

    monkeypatch.setattr(convergence.time, "sleep", lambda _: None)


def test_measures_both_planes_and_observes_control_plane_and_resolution(fake, no_sleep) -> None:  # type: ignore[no-untyped-def]
    from tests.unit.fakes import make_healthy

    lab, parts = fake
    make_healthy(lab, parts)  # r1 routes 10.0.30.0/24 via the primary until p2 goes down
    r1 = lab["r1"]
    result = measure_reconvergence(
        check="reconverge_local_link",
        src=lab["h1"],
        dst=lab["h4"],
        router=r1,
        prefix="10.0.30.0/24",
        expected_next_hop="10.0.12.3",
        action=lambda: r1.links.set_link("p2", up=False),
    )
    # the fake stream sends 1000 and gets 990 back, with no elapsed time: 10 lost x 0.01 s
    assert result.data_plane_s == 0.1
    assert result.interval_s == 0.01
    assert result.control_plane_s >= 0
    # observed without an SLA; the data-plane outage is recorded by the test, not here
    assert [(m.check, m.sla_s, m.breached) for m in lab.timing.measurements] == [
        ("reconverge_local_link_control_plane", None, False),
        ("reconverge_local_link_stream_interval", None, False),
    ]


def test_outage_uses_the_interval_the_stream_really_achieved() -> None:
    """On a busy machine ping sends slower than asked: 300 probes over 5.98 s is one every
    20 ms, not 10, so 10 lost probes are 0.2 s of outage, not 0.1."""
    from netcheck.capabilities import PingResult
    from netcheck.convergence import outage_seconds, stream_interval

    slow = PingResult("h1", "stream", 300, 290, elapsed_s=5.98)
    assert stream_interval(slow, 0.01) == pytest.approx(0.02)
    assert outage_seconds(slow, 0.01) == 0.2
    unknown = PingResult("h1", "stream", 300, 290)  # BusyBox: no elapsed time
    assert outage_seconds(unknown, 0.01) == 0.1


def test_parse_ping_reads_the_elapsed_time() -> None:
    from netcheck.backends.local import parse_ping

    iputils = "300 packets transmitted, 290 received, 3.3% packet loss, time 3012ms\n"
    assert parse_ping("h1", "x", iputils).elapsed_s == 3.012
    errors = "15 packets transmitted, 0 received, +15 errors, 100% packet loss, time 2912ms"
    assert parse_ping("h1", "x", errors).elapsed_s == 2.912
    busybox = "3 packets transmitted, 3 packets received, 0% packet loss\n"
    assert parse_ping("h1", "x", busybox).elapsed_s is None


def test_observe_records_a_value_without_an_sla() -> None:
    timing = Timing("unit", {})
    m = timing.observe("anything_control_plane", 0.12345)
    assert (m.check, m.value_s, m.sla_s) == ("anything_control_plane", 0.123, None)
    with pytest.raises(KeyError):
        timing.record("typo_check", 1.0)  # an SLA check must exist: no silent skip


def test_repeats_come_from_the_environment(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    load = lambda env: Timing.load(root / "timing.yaml", "local", {"dead_interval": 4.0}, env)  # noqa: E731
    assert load({}).repeats == 1
    assert load({"NETCHECK_REPEATS": "5"}).repeats == 5
    for bad in ("0", "-1", "five"):
        with pytest.raises(ValueError, match="NETCHECK_REPEATS"):
            load({"NETCHECK_REPEATS": bad})


def test_failover_repeats_and_holds_the_median_to_the_sla(pytester, no_sleep) -> None:  # type: ignore[no-untyped-def]
    """The real failover test with 3 runs: each run starts on the primary path (the link
    comes back in between), and the median outage meets the SLA, not each run. One run of
    6 s (600 probes lost at 10 ms) among 0.1 s and 0.2 s passes the 5 s SLA."""
    from tests.unit.test_review_fixes import _run_lab_suite

    result = _run_lab_suite(
        pytester,
        False,
        "-k",
        "reroutes_via_backup",
        "-s",
        repeats=3,
        lost_per_stream={"h1": [10, 600, 20]},
    )
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "*run 1: data-plane outage 0.1s*",
            "run 2: data-plane outage 6.0s*",
            "run 3: data-plane outage 0.2s*",
        ]
    )


def test_a_median_over_the_sla_fails(pytester, no_sleep) -> None:  # type: ignore[no-untyped-def]
    from tests.unit.test_review_fixes import _run_lab_suite

    result = _run_lab_suite(
        pytester,
        False,
        "-k",
        "reroutes_via_backup",
        repeats=3,
        lost_per_stream={"h1": [600, 10, 700]},
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*AssertionError: reconverge_local_link: measured 6.000s exceeds SLA 5.000s",
            "*7. Bring r1:p2 back up for the next run [ok*",
            "*8. Check r1 reaches 10.0.30.0/24 via the primary (10.0.12.1) (run 3 of 3) [ok*",
            "*10. Check the outage (median of 3 runs, 6.0s) is within the 5s SLA [FAILED*",
        ]
    )
