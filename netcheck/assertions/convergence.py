"""Convergence measurement (ADR-0004).

Data plane: loss-derived, as in RFC 6413. A ping stream runs every `interval`
seconds through the failure; outage = lost packets x the interval the stream really
achieved (ping's elapsed time / gaps), which on a busy machine is longer than asked for.
The achieved interval is also the measurement's resolution, and is recorded.
Control plane: time until the route points at the expected next hop.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from netcheck.assertions.common import attempt, evidence, fail, ospf_neighbor_lines
from netcheck.devices.device import Device
from netcheck.model.capabilities import PingResult
from netcheck.run.lab import current
from netcheck.run.report import FailureReport
from netcheck.run.timing import WaitTimeout


@dataclass
class Convergence:
    check: str
    data_plane_s: float
    control_plane_s: float
    stream: PingResult
    interval_s: float  # what one lost probe stood for: the resolution of data_plane_s


def stream_interval(stream: PingResult, asked: float) -> float:
    """The achieved interval when ping reported its elapsed time, else the one asked for."""
    return stream.interval_s or asked


def outage_seconds(stream: PingResult, interval: float) -> float:
    """Lost probes x the achieved interval (`interval` when ping reported no time)."""
    return round(stream.loss * stream_interval(stream, interval), 3)


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
    """Run `action` (the failure) while measuring both convergence numbers.

    Only measures. Check the starting path first (assert_route_uses with
    why="before the failure, "), or a lab already on the backup path "converges" in zero
    seconds; record the data-plane outage against its SLA as a step of its own
    (lab.timing.record). The control-plane time is recorded here, with no SLA.
    """
    lab = current()
    th = lab.timing[check]
    interval = th.stream_interval or 0.01
    dst_ip = lab.intent.host_ip(dst.name)

    def next_hops() -> tuple[str, ...]:
        route = router.ospf.routes().get(prefix)
        return route.next_hops if route and route.installed else ()

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
        fail(report)

    time.sleep(1.0)  # let the stream show recovery before stopping it
    stream = src.shell.stop_stream(token)
    if stream.received == 0:
        fail(
            FailureReport(
                verdict=f"traffic {src.name} -> {dst.name} must recover after the failure",
                expected="replies after reconvergence",
                actual="0 replies in the whole stream",
            )
        )
    lab.timing.observe(f"{check}_control_plane", cp)
    achieved = stream_interval(stream, interval)
    lab.timing.observe(f"{check}_stream_interval", achieved)
    return Convergence(
        check, outage_seconds(stream, interval), round(cp, 3), stream, round(achieved, 4)
    )


def _stop_quietly(src: Device, token: str) -> None:
    with contextlib.suppress(Exception):  # already stopped is fine
        src.shell.stop_stream(token)
