"""Reconvergence measurement on the healthy fake lab (ADR-0004)."""

from __future__ import annotations

import pytest

from netcheck.assertions.common import ospf_neighbor_lines
from netcheck.capabilities import PingResult, Route
from netcheck.convergence import measure_reconvergence, outage_seconds
from netcheck.report import IntentViolation
from netcheck.timing import Threshold, Timing
from netcheck.transport.base import TransportError


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


# --- what the measurement sends, waits for and reports (mutation floor, improvement 6) ---

PRIMARY, BACKUP = "10.0.12.1", "10.0.12.3"
PREFIX = "10.0.30.0/24"


class StreamSpy:
    """Stands in for h1's ping stream: records what was asked and stopped."""

    def __init__(self, result: PingResult | None = None) -> None:
        self.started: list[tuple[str, float, float]] = []
        self.stopped: list[str] = []
        self.result = result or PingResult("h1", "stream", 1000, 990)

    def start(self, dst: str, interval: float, deadline: float) -> str:
        self.started.append((dst, interval, deadline))
        return "tok-7"

    def stop(self, token: str) -> PingResult:
        self.stopped.append(token)
        return self.result


def _route(*next_hops: str, installed: bool = True) -> dict[str, Route]:
    return {PREFIX: Route(PREFIX, "ospf", next_hops, installed)}


def _measure(lab, parts, monkeypatch, routes, spy, **threshold):  # type: ignore[no-untyped-def]
    """measure_reconvergence on r1 with scripted route reads and a short timeout."""
    answers = list(routes)
    monkeypatch.setattr(
        parts["ospf_r1"], "routes", lambda: answers.pop(0) if len(answers) > 1 else answers[0]
    )
    monkeypatch.setattr(lab["h1"].shell, "start_stream", spy.start)
    monkeypatch.setattr(lab["h1"].shell, "stop_stream", spy.stop)
    th = {"timeout": 10.0, "poll": 0.001, "stream_interval": 0.01} | threshold
    lab.timing.thresholds["reconverge_local_link"] = Threshold("reconverge_local_link", **th)
    result = measure_reconvergence(
        check="reconverge_local_link",
        src=lab["h1"],
        dst=lab["h4"],
        router=lab["r1"],
        prefix=PREFIX,
        expected_next_hop=BACKUP,
        action=lambda: None,
    )
    return result, answers


def test_the_stream_targets_dst_at_the_threshold_interval_and_outlives_the_timeout(
    fake, no_sleep, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    spy = StreamSpy()
    result, _ = _measure(lab, parts, monkeypatch, [_route(BACKUP)], spy, stream_interval=0.02)
    assert spy.started == [(lab.intent.host_ip("h4"), 0.02, 15.0)]  # timeout 10 + 5
    assert spy.stopped == ["tok-7"]  # the stream it started, not another
    # 10 lost at the asked 20 ms (the spy's stream has no elapsed time)
    assert (result.check, result.data_plane_s, result.interval_s) == (
        "reconverge_local_link",
        0.2,
        0.02,
    )


def test_cleanup_stops_the_stream_and_tolerates_one_already_stopped(
    fake, no_sleep, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    spy = StreamSpy()
    _measure(lab, parts, monkeypatch, [_route(BACKUP)], spy)
    assert lab.cleanup.run() == []
    assert spy.stopped == ["tok-7", "tok-7"]  # measured, then stopped again by cleanup

    def gone(token: str) -> PingResult:
        raise TransportError("h1: no such process")

    spy2 = StreamSpy()
    _measure(lab, parts, monkeypatch, [_route(BACKUP)], spy2)
    monkeypatch.setattr(lab["h1"].shell, "stop_stream", gone)
    assert lab.cleanup.run() == []  # already stopped is fine, not a cleanup error


def test_waits_for_the_expected_next_hop_on_an_installed_route(fake, no_sleep, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Still on the primary, then the backup known but not installed, then installed:
    only the last one ends the wait."""
    lab, parts = fake
    reads = [_route(PRIMARY), _route(BACKUP, installed=False), {}, _route(BACKUP)]
    _, left = _measure(lab, parts, monkeypatch, reads, StreamSpy())
    assert left == [_route(BACKUP)]  # every read before it was consumed


def test_a_route_that_never_moves_fails_with_the_next_hops_neighbors_and_outage(
    fake, no_sleep, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from tests.unit.fakes import make_healthy

    lab, parts = fake
    make_healthy(lab, parts)  # r1 has Full neighbors to show as evidence
    spy = StreamSpy(PingResult("h1", "stream", 1000, 700))
    with pytest.raises(IntentViolation) as exc:
        _measure(lab, parts, monkeypatch, [_route(PRIMARY)], spy, timeout=0.05)
    report = exc.value.report
    assert report.verdict == (f"r1 must reroute {PREFIX} via {BACKUP} within 0s after the failure")
    assert report.expected == f"next hop {BACKUP}"
    assert report.actual.endswith(f"next hops ('{PRIMARY}',)")
    assert report.evidence["r1 neighbors"] == ospf_neighbor_lines(lab["r1"]) != []
    assert report.measurements == {"data_plane_outage": 3.0}  # 300 lost x 10 ms
    assert spy.stopped == ["tok-7"]


def test_a_stream_that_cannot_be_read_after_a_timeout_still_reports(
    fake, no_sleep, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    spy = StreamSpy()

    def broken(token: str) -> PingResult:
        raise TransportError("h1: docker exec failed")

    spy.stop = broken  # type: ignore[method-assign]
    with pytest.raises(IntentViolation) as exc:
        _measure(lab, parts, monkeypatch, [_route(PRIMARY)], spy, timeout=0.05)
    report = exc.value.report
    assert report.measurements == {}
    assert report.evidence["evidence unavailable"] == [
        "ping stream on h1: TransportError: h1: docker exec failed"
    ]


@pytest.mark.parametrize(("received", "recovered"), [(0, False), (1, True)])
def test_traffic_must_recover_one_reply_is_enough(
    fake, no_sleep, monkeypatch, received: int, recovered: bool
) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    spy = StreamSpy(PingResult("h1", "stream", 1000, received))
    if recovered:
        _measure(lab, parts, monkeypatch, [_route(BACKUP)], spy)
        return
    with pytest.raises(IntentViolation) as exc:
        _measure(lab, parts, monkeypatch, [_route(BACKUP)], spy)
    report = exc.value.report
    assert (report.verdict, report.expected, report.actual) == (
        "traffic h1 -> h4 must recover after the failure",
        "replies after reconvergence",
        "0 replies in the whole stream",
    )


def test_results_are_rounded_to_milliseconds_and_the_interval_to_a_tenth_of_one(
    fake, no_sleep, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """7 lost at 12.3456 ms is 86.4 ms of outage, reported as 0.086 s; the control-plane
    time 0.123456 s as 0.123 s."""
    lab, parts = fake
    spy = StreamSpy(PingResult("h1", "stream", 1001, 994, elapsed_s=12.3456))
    monkeypatch.setattr(lab.timing, "wait_until", lambda check, probe, ok: (probe(), 0.123456))
    result, _ = _measure(lab, parts, monkeypatch, [_route(BACKUP)], spy)
    assert (result.data_plane_s, result.control_plane_s, result.interval_s) == (
        0.086,
        0.123,
        0.0123,
    )
    assert outage_seconds(spy.result, 0.01) == 0.086
