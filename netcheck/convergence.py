"""Convergence measurement (ADR-0004).

Data plane: loss-derived, as in RFC 6413. A ping stream runs every `interval`
seconds through the failure; outage = lost packets x interval.
Control plane: time until the route points at the expected next hop.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from netcheck.capabilities import PingResult
from netcheck.device import Device
from netcheck.lab import current
from netcheck.report import FailureReport, IntentViolation
from netcheck.timing import Measurement, WaitTimeout


@dataclass
class Convergence:
    check: str
    data_plane_s: float
    control_plane_s: float
    stream: PingResult


def outage_seconds(stream: PingResult, interval: float) -> float:
    return round(stream.loss * interval, 3)


def measure_reconvergence(
    *,
    check: str,
    src: Device,
    dst: Device,
    router: Device,
    prefix: str,
    expected_next_hop: str,
    action: Callable[[], None],
) -> Convergence:
    """Run `action` (the failure) while measuring both convergence numbers."""
    lab = current()
    th = lab.timing[check]
    interval = th.stream_interval or 0.01
    dst_ip = lab.intent.host_ip(dst.name)

    token = src.shell.start_stream(dst_ip, interval=interval, deadline=th.timeout + 5)
    lab.cleanup.register(f"stop ping stream on {src.name}", lambda: _stop_quietly(src, token))
    time.sleep(0.5)  # baseline traffic before the failure

    action()

    def next_hops() -> tuple[str, ...]:
        route = router.ospf.routes().get(prefix)
        return route.next_hops if route and route.installed else ()

    try:
        _, cp = lab.timing.wait_until(check, next_hops, lambda nh: expected_next_hop in nh)
    except WaitTimeout as exc:
        stream = src.shell.stop_stream(token)
        report = FailureReport(
            verdict=(
                f"{router.name} must reroute {prefix} via {expected_next_hop} "
                f"within {th.timeout:.0f}s after the failure"
            ),
            expected=f"next hop {expected_next_hop}",
            actual=f"next hops {exc.last!r}",
        )
        report.add_evidence(
            f"{router.name} neighbors",
            [f"{n.router_id} {n.state} via {n.port}" for n in router.ospf.neighbors()],
        )
        report.measurements["data_plane_outage"] = outage_seconds(stream, interval)
        raise IntentViolation(report) from exc

    time.sleep(1.0)  # let the stream show recovery before stopping it
    stream = src.shell.stop_stream(token)
    if stream.received == 0:
        raise IntentViolation(
            FailureReport(
                verdict=f"traffic {src.name} -> {dst.name} must recover after the failure",
                expected="replies after reconvergence",
                actual="0 replies in the whole stream",
            )
        )
    data_plane = outage_seconds(stream, interval)
    lab.timing.record(check, data_plane)
    lab.timing.measurements.append(Measurement(f"{check}_control_plane", round(cp, 3), None, False))
    return Convergence(check, data_plane, round(cp, 3), stream)


def _stop_quietly(src: Device, token: str) -> None:
    with contextlib.suppress(Exception):  # already stopped is fine
        src.shell.stop_stream(token)
