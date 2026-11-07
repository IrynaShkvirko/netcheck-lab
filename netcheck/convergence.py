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

from netcheck.assertions.common import attempt, evidence, ospf_neighbor_lines
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
    before_next_hop: str | None = None,
    enforce_sla: bool = True,
) -> Convergence:
    """Run `action` (the failure) while measuring both convergence numbers.

    `before_next_hop` is the path traffic must use before the failure. Without that
    check, a lab already on the backup path would "converge" in zero seconds.
    """
    lab = current()
    th = lab.timing[check]
    interval = th.stream_interval or 0.01
    dst_ip = lab.intent.host_ip(dst.name)

    def next_hops() -> tuple[str, ...]:
        route = router.ospf.routes().get(prefix)
        return route.next_hops if route and route.installed else ()

    if before_next_hop is not None:
        try:
            lab.timing.wait_until("route_installed", next_hops, lambda nh: before_next_hop in nh)
        except WaitTimeout as exc:
            report = FailureReport(
                verdict=(
                    f"before the failure, {router.name} must route {prefix} via {before_next_hop}"
                ),
                expected=f"next hop {before_next_hop}",
                actual=exc.describe(f"next hops {exc.last!r}"),
            )
            report.add_evidence(
                "diagnosis",
                [
                    "the failover would start from the wrong path, so its result would "
                    "mean nothing (check OSPF costs)"
                ],
            )
            raise IntentViolation(report) from exc

    token = src.shell.start_stream(dst_ip, interval=interval, deadline=th.timeout + 5)
    lab.cleanup.register(f"stop ping stream on {src.name}", lambda: _stop_quietly(src, token))
    time.sleep(0.5)  # baseline traffic before the failure

    action()

    try:
        _, cp = lab.timing.wait_until(check, next_hops, lambda nh: expected_next_hop in nh)
    except WaitTimeout as exc:
        report = FailureReport(
            verdict=(
                f"{router.name} must reroute {prefix} via {expected_next_hop} "
                f"within {th.timeout:.0f}s after the failure"
            ),
            expected=f"next hop {expected_next_hop}",
            actual=exc.describe(f"next hops {exc.last!r}"),
        )
        evidence(report, f"{router.name} neighbors", lambda: ospf_neighbor_lines(router))
        stream = attempt(
            report, f"ping stream on {src.name}", lambda: src.shell.stop_stream(token), None
        )
        if stream is not None:
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
    if enforce_sla:  # False: the caller records it (and enforces the SLA) as its own step
        lab.timing.record(check, data_plane)
    lab.timing.measurements.append(Measurement(f"{check}_control_plane", round(cp, 3), None, False))
    return Convergence(check, data_plane, round(cp, 3), stream)


def _stop_quietly(src: Device, token: str) -> None:
    with contextlib.suppress(Exception):  # already stopped is fine
        src.shell.stop_stream(token)
