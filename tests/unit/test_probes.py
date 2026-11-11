"""Probes that did not run, and transport errors while polling (ADR-0002, ADR-0004)."""

from __future__ import annotations

import pytest

from netcheck.assertions import assert_isolated, assert_ospf_neighbor_full, assert_reachable
from netcheck.devices.transport.base import TransportError
from netcheck.model.capabilities import CaptureHandle, PingResult
from netcheck.run.report import IntentViolation
from netcheck.run.timing import Threshold, Timing, WaitTimeout

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


# --- assert_reachable: what it sends, when it stops, what it reports (improvement 6) ---


def _short_reachability(lab, timeout: float = 0.05) -> None:  # type: ignore[no-untyped-def]
    lab.timing.thresholds["reachability"] = Threshold("reachability", timeout=timeout, poll=0.001)


def test_reachability_sends_three_probes_200ms_apart_within_3s(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    calls: list[tuple[str, dict[str, float]]] = []

    def ping(dst: str, **kw: float) -> PingResult:
        calls.append((dst, kw))
        return PingResult("h1", dst, 3, 3)

    monkeypatch.setattr(lab["h1"].shell, "ping", ping)
    assert_reachable(lab["h1"], lab["h4"])
    assert calls == [(lab.intent.host_ip("h4"), {"count": 3, "interval": 0.2, "deadline": 3})]


def test_reachability_needs_one_reply_from_a_ping_that_ran(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No replies, or replies on a ping that reported an error, is "not yet"; one reply
    is enough."""
    lab, _ = fake
    dst = lab.intent.host_ip("h4")
    answers = [
        PingResult("h1", dst, 3, 0),
        PingResult("h1", dst, 3, 2, error="ping did not run (rc=2)"),
        PingResult("h1", dst, 3, 1),
    ]
    monkeypatch.setattr(lab["h1"].shell, "ping", lambda *a, **k: answers.pop(0))
    assert assert_reachable(lab["h1"], lab["h4"]).received == 1
    assert answers == []


def test_unreachable_reports_replies_neighbors_and_every_routers_routes(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions.common import arp_line
    from tests.unit.fakes import make_healthy

    lab, parts = fake
    make_healthy(lab, parts)
    _short_reachability(lab)
    dst = lab.intent.host_ip("h4")
    parts["shell_h1"].neigh = {"10.0.10.1": "REACHABLE"}  # h1 resolved its gateway
    monkeypatch.setattr(lab["h1"].shell, "ping", lambda *a, **k: PingResult("h1", dst, 3, 0))
    with pytest.raises(IntentViolation) as exc:
        assert_reachable(lab["h1"], lab["h4"])
    report = exc.value.report
    assert (report.verdict, report.expected, report.actual) == (
        f"h1 must reach h4 ({dst})",
        f"ICMP replies from {dst} within 0s",
        "0/3 replies",
    )
    arp = [arp_line(n) for n in lab["h1"].shell.neighbors()]
    routers = [r.name for r in lab.by_role("router")]
    assert list(report.evidence) == ["h1 neighbor table"] + [f"{r} OSPF routes" for r in routers]
    assert report.evidence["h1 neighbor table"] == arp != []
    for r in routers:
        assert report.evidence[f"{r} OSPF routes"] == sorted(lab[r].ospf.routes()) != []


def test_unreachable_while_the_host_does_not_answer_says_why(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    _short_reachability(lab)

    def broken(*a: object, **k: object) -> PingResult:
        raise TransportError("h1: docker exec failed")

    monkeypatch.setattr(lab["h1"].shell, "ping", broken)
    with pytest.raises(IntentViolation) as exc:
        assert_reachable(lab["h1"], lab["h4"])
    assert exc.value.report.actual == "no answer: h1: docker exec failed"


@pytest.mark.parametrize(
    ("window", "count", "deadline"), [(2.0, 10, 2.0), (None, 20, 4.0), (0.2, 1, 0.2)]
)
def test_isolation_probes_run_for_the_whole_window(  # type: ignore[no-untyped-def]
    fake, monkeypatch, window, count: int, deadline: float
) -> None:
    """One probe every 200 ms for the isolation window (4 s when timing.yaml sets none),
    with ping's deadline at the window so it never stops early."""
    from netcheck.assertions.l2 import send_isolation_probes

    lab, _ = fake
    lab.timing.thresholds["isolation_window"] = Threshold(
        "isolation_window", timeout=0, poll=0, window=window
    )
    calls: list[tuple[str, dict[str, float]]] = []

    def ping(dst: str, **kw: float) -> PingResult:
        calls.append((dst, kw))
        return PingResult("h1", dst, count, 0)

    monkeypatch.setattr(lab["h1"].shell, "ping", ping)
    assert send_isolation_probes(lab["h1"], lab["h3"]).sent == count
    assert calls == [
        (lab.intent.host_ip("h3"), {"count": count, "interval": 0.2, "deadline": deadline})
    ]


@pytest.mark.parametrize(
    ("result", "actual"),
    [(NOT_RUN, NOT_RUN.error), (PingResult("h1", "10.0.30.14", 0, 0), "0 probes sent")],
)
def test_isolation_probes_that_never_left_are_inconclusive(  # type: ignore[no-untyped-def]
    fake, monkeypatch, result: PingResult, actual: str
) -> None:
    from netcheck.assertions.l2 import isolation_probe_count, send_isolation_probes

    lab, _ = fake
    monkeypatch.setattr(lab["h1"].shell, "ping", lambda *a, **k: result)
    with pytest.raises(IntentViolation) as exc:
        send_isolation_probes(lab["h1"], lab["h3"])
    report = exc.value.report
    assert (report.verdict, report.expected, report.actual) == (
        "isolation h1-h3 inconclusive: no probes were sent",
        f"{isolation_probe_count()} ICMP probes sent, 0 replies",
        actual,
    )
