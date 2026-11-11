"""The evidence helpers every failure report is built from (netcheck.assertions.common):
neighbor lines, best-effort steps, and the packet capture around a check."""

from __future__ import annotations

from pathlib import Path

import pytest

from netcheck.assertions.common import (
    CAPTURE_LINES,
    arp_line,
    capture_dir,
    evidence,
    finish_capture,
    start_capture,
)
from netcheck.capabilities import CaptureHandle
from netcheck.report import FailureReport
from netcheck.transport.base import TransportError


@pytest.mark.parametrize(
    ("entry", "line"),
    [
        (
            {"dst": "10.0.10.12", "lladdr": "aa:bb:cc:00:00:02", "state": ["REACHABLE"]},
            "10.0.10.12 lladdr aa:bb:cc:00:00:02 REACHABLE",
        ),
        (
            {"dst": "10.0.10.1", "lladdr": "aa:bb", "state": ["STALE", "PROBE"]},
            "10.0.10.1 lladdr aa:bb STALE,PROBE",
        ),
        ({"dst": "10.0.20.13", "state": "FAILED"}, "10.0.20.13 lladdr None FAILED"),
    ],
)
def test_arp_line_shows_address_mac_and_every_state(entry: dict[str, object], line: str) -> None:
    assert arp_line(entry) == line


def test_evidence_that_cannot_be_read_says_which_and_why() -> None:
    report = FailureReport("v")

    def broken() -> list[str]:
        raise TransportError("r1: docker exec failed")

    evidence(report, "r1 neighbors", broken)
    evidence(report, "r2 neighbors", lambda: [])
    assert report.evidence == {
        "evidence unavailable": ["r1 neighbors: TransportError: r1: docker exec failed"],
        "r2 neighbors": ["(none)"],
    }


def test_capture_dir_creates_the_whole_path(fake, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    lab.run.artifacts = tmp_path / "deep" / "test-1"
    assert capture_dir(lab) == tmp_path / "deep" / "test-1"
    assert capture_dir(lab).is_dir()  # again: already there is fine


class CaptureSpy:
    def __init__(self, packets: int, local: str | None = "/art/h3-p1.pcap") -> None:
        self.started: list[tuple[str, str]] = []
        self.stopped: list[tuple[CaptureHandle, str]] = []
        self.packets, self.local = packets, local

    def start(self, port: str, bpf: str = "") -> CaptureHandle:
        self.started.append((port, bpf))
        return CaptureHandle("h3", port, "/tmp/c.pcap", "42")

    def stop(self, handle: CaptureHandle, dest: str) -> CaptureHandle:
        self.stopped.append((handle, dest))
        summary = [f"packet {i}" for i in range(1, self.packets + 1)]
        return CaptureHandle("h3", handle.port, "/tmp/c.pcap", "42", self.local, summary, True)


def _spy(lab, monkeypatch, spy: CaptureSpy) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(lab["h3"].shell, "start_capture", spy.start)
    monkeypatch.setattr(lab["h3"].shell, "stop_capture", spy.stop)


def test_a_capture_is_stopped_by_cleanup_into_the_test_folder(fake, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    lab.run.artifacts = tmp_path / "t"
    spy = CaptureSpy(1)
    _spy(lab, monkeypatch, spy)
    handle = start_capture(lab, lab["h3"], "p1", "icmp")
    assert spy.started == [("p1", "icmp")]
    assert lab.cleanup.run() == []
    assert spy.stopped == [(handle, str(tmp_path / "t"))]


def test_a_capture_that_cannot_start_is_no_failure(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake

    def broken(port: str, bpf: str = "") -> CaptureHandle:
        raise TransportError("h3: tcpdump missing")

    monkeypatch.setattr(lab["h3"].shell, "start_capture", broken)
    assert start_capture(lab, lab["h3"], "p1", "icmp") is None
    assert len(lab.cleanup) == 0


@pytest.mark.parametrize(
    ("packets", "more"), [(CAPTURE_LINES + 3, 3), (CAPTURE_LINES + 1, 1), (CAPTURE_LINES, 0)]
)
def test_a_report_shows_the_first_packets_and_counts_the_rest(  # type: ignore[no-untyped-def]
    fake, monkeypatch, tmp_path, packets: int, more: int
) -> None:
    lab, _ = fake
    lab.run.artifacts = tmp_path / "t"
    spy = CaptureSpy(packets)
    _spy(lab, monkeypatch, spy)
    handle = CaptureHandle("h3", "p1", "/tmp/c.pcap", "42")
    report = FailureReport("v", capture_summary=["sw1:p4", "packet 0"])
    finish_capture(lab, lab["h3"], handle, report)
    shown = [f"packet {i}" for i in range(1, CAPTURE_LINES + 1)]
    tail = [f"... {more} more packets in the pcap"] if more else []
    assert report.capture_summary == ["sw1:p4", "packet 0", "h3:p1", *shown, *tail]
    assert report.capture_files == ["/art/h3-p1.pcap"]
    assert spy.stopped == [(handle, str(tmp_path / "t"))]


def test_a_capture_that_cannot_be_stopped_is_noted_not_raised(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake

    def broken(handle: CaptureHandle, dest: str) -> CaptureHandle:
        raise TransportError("h3: gone")

    monkeypatch.setattr(lab["h3"].shell, "stop_capture", broken)
    report = FailureReport("v")
    finish_capture(lab, lab["h3"], CaptureHandle("h3", "p1", "/tmp/c.pcap", "42"), report)
    assert report.evidence == {"evidence unavailable": ["capture h3:p1: TransportError: h3: gone"]}
    assert (report.capture_summary, report.capture_files) == ([], [])
    finish_capture(lab, lab["h3"], None, report)  # no capture was started: nothing to add
    assert report.evidence == {"evidence unavailable": ["capture h3:p1: TransportError: h3: gone"]}
