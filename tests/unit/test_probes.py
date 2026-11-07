"""Probes that did not run, and transport errors while polling (ADR-0002, ADR-0004)."""

from __future__ import annotations

import pytest

from netcheck.assertions import assert_isolated, assert_ospf_neighbor_full, assert_reachable
from netcheck.capabilities import CaptureHandle, PingResult
from netcheck.report import IntentViolation
from netcheck.timing import Threshold, Timing, WaitTimeout
from netcheck.transport.base import TransportError

NOT_RUN = PingResult(
    "h1", "10.0.30.14", 0, 0, error="ping did not run (rc=2): Network is unreachable"
)


def test_reachability_retries_while_the_ping_cannot_run(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    answers = [NOT_RUN, NOT_RUN, PingResult("h1", "10.0.30.14", 3, 3)]
    monkeypatch.setattr(lab["h1"].shell, "ping", lambda *a, **k: answers.pop(0))
    assert assert_reachable(lab["h1"], lab["h4"]).received == 3
    assert answers == []


def test_reachability_reports_why_the_ping_never_ran(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    monkeypatch.setattr(lab["h1"].shell, "ping", lambda *a, **k: NOT_RUN)
    with pytest.raises(IntentViolation) as exc:
        assert_reachable(lab["h1"], lab["h4"])
    assert exc.value.report.actual == "ping did not run (rc=2): Network is unreachable"


def test_isolation_probe_that_did_not_run_is_inconclusive_with_the_reason(
    fake, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    monkeypatch.setattr(lab["h1"].shell, "ping", lambda *a, **k: NOT_RUN)
    with pytest.raises(IntentViolation, match="inconclusive") as exc:
        assert_isolated(lab["h1"], lab["h3"])
    assert "Network is unreachable" in exc.value.report.actual


def _timing() -> Timing:
    return Timing("unit", {"x": Threshold("x", timeout=1.0, poll=0.1)})


def test_wait_until_retries_transport_errors() -> None:
    answers: list[object] = [TransportError("r1: docker exec failed"), "Full"]

    def probe() -> str:
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return str(a)

    value, _ = _timing().wait_until("x", probe, lambda s: s == "Full", sleep=lambda _: None)
    assert value == "Full"


def test_wait_until_times_out_with_the_last_transport_error() -> None:
    now = [0.0]

    def probe() -> str:
        raise TransportError("r1: docker exec failed")

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(WaitTimeout) as exc:
        _timing().wait_until("x", probe, bool, sleep=sleep, clock=lambda: now[0])
    assert str(exc.value.error) == "r1: docker exec failed"
    assert exc.value.describe("state Init") == "no answer: r1: docker exec failed"


def test_wait_until_does_not_hide_other_errors() -> None:
    def probe() -> str:
        raise KeyError("a bug in netcheck")

    with pytest.raises(KeyError):
        _timing().wait_until("x", probe, bool, sleep=lambda _: None)


def test_unreadable_neighbor_table_still_gives_a_report(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake

    def unreachable() -> list[object]:
        raise TransportError("r1: docker exec failed")

    monkeypatch.setattr(parts["ospf_r1"], "neighbors", unreachable)
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_neighbor_full(lab["r1"], lab["r2"], port="p2")
    assert exc.value.report.actual == "no answer: r1: docker exec failed"


def test_stopping_a_capture_twice_changes_nothing(fake, tmp_path) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    shell = lab["h3"].shell
    handle = shell.stop_capture(CaptureHandle("h3", "p1", "/tmp/x.pcap", "1"), str(tmp_path))
    first = (list(handle.summary), handle.local_path)
    again = shell.stop_capture(handle, str(tmp_path / "elsewhere"))
    assert again is handle and (list(again.summary), again.local_path) == first
