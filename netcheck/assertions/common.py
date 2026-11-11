"""Shared pieces of the assertions: failing, guarded evidence collection, captures."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NoReturn, TypeVar

from netcheck.devices.device import Device
from netcheck.model.capabilities import CaptureHandle, OspfNeighbor
from netcheck.run.lab import Lab
from netcheck.run.report import FailureReport, IntentViolation

T = TypeVar("T")

CAPTURE_LINES = 6  # packets shown in the report; the full capture is in the pcap


def fail(report: FailureReport) -> NoReturn:
    raise IntentViolation(report)


def attempt(report: FailureReport, what: str, fn: Callable[[], T], default: T) -> T:
    """Run one evidence step. If it fails, record that in the report and go on.

    The device that broke is often the one being asked for evidence; an error here
    must never replace the verdict.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - evidence is best effort by design
        report.add_evidence("evidence unavailable", [f"{what}: {type(exc).__name__}: {exc}"])
        return default


def evidence(report: FailureReport, title: str, fn: Callable[[], list[str]]) -> None:
    """Add an evidence section; '(none)' if it is empty, a note if it failed."""
    lines = attempt(report, title, fn, None)
    if lines is not None:
        report.add_evidence(title, lines or ["(none)"])


def neighbor_line(n: OspfNeighbor) -> str:
    """One OSPF neighbor as an evidence line ('2.2.2.2 Full via p2')."""
    return f"{n.router_id} {n.state} via {n.port}"


def ospf_neighbor_lines(device: Device) -> list[str]:
    return [neighbor_line(n) for n in device.ospf.neighbors()]


def arp_line(entry: Mapping[str, object]) -> str:
    state = entry.get("state")
    state_s = ",".join(map(str, state)) if isinstance(state, list) else str(state)
    return f"{entry.get('dst')} lladdr {entry.get('lladdr')} {state_s}"


def capture_dir(lab: Lab) -> Path:
    path = lab.test_artifacts
    path.mkdir(parents=True, exist_ok=True)
    return path


def start_capture(lab: Lab, device: Device, port: str, bpf: str) -> CaptureHandle | None:
    try:
        handle = device.shell.start_capture(port, bpf)
    except Exception:  # noqa: BLE001 - capture is evidence, never the reason a test fails
        return None
    lab.cleanup.register(
        f"stop capture {device.name}:{port}",
        lambda: device.shell.stop_capture(handle, str(capture_dir(lab))),
    )
    return handle


def finish_capture(
    lab: Lab, device: Device, handle: CaptureHandle | None, report: FailureReport
) -> None:
    if handle is None:
        return
    done = attempt(
        report,
        f"capture {device.name}:{handle.port}",
        lambda: device.shell.stop_capture(handle, str(capture_dir(lab))),
        None,
    )
    if done is None:
        return
    shown = done.summary[:CAPTURE_LINES]
    more = len(done.summary) - len(shown)
    report.capture_summary += [f"{device.name}:{handle.port}", *shown]
    if more > 0:
        report.capture_summary.append(f"... {more} more packets in the pcap")
    if done.local_path:
        report.capture_files.append(done.local_path)
