"""Layer 3 reachability between hosts."""

from __future__ import annotations

from functools import partial

from netcheck.assertions.common import arp_line, evidence, fail
from netcheck.devices.device import Device
from netcheck.model.capabilities import PingResult
from netcheck.run.lab import current
from netcheck.run.report import FailureReport
from netcheck.run.timing import WaitTimeout


def assert_reachable(a: Device, b: Device) -> PingResult:
    """a reaches b's address. Retries until the `reachability` timeout, also while the
    ping cannot run yet (no route to the network while OSPF converges)."""
    lab = current()
    dst = lab.intent.host_ip(b.name)
    th = lab.timing["reachability"]
    try:
        result, _ = lab.timing.wait_until(
            "reachability",
            lambda: a.shell.ping(dst, count=3, interval=0.2, deadline=3),
            lambda r: r.ran and r.received > 0,  # a ping that did not run: try again
        )
    except WaitTimeout as exc:
        last = exc.last if isinstance(exc.last, PingResult) else None
        if last is None:
            actual = exc.describe("no result")
        elif not last.ran:
            actual = last.error
        else:
            actual = f"{last.received}/{last.sent} replies"
        report = FailureReport(
            verdict=f"{a.name} must reach {b.name} ({dst})",
            expected=f"ICMP replies from {dst} within {th.timeout:.0f}s",
            actual=actual,
        )
        evidence(report, f"{a.name} neighbor table", partial(_arp_table, a))
        for r in lab.by_role("router"):
            evidence(report, f"{r.name} OSPF routes", partial(_route_prefixes, r))
        fail(report)
    return result


def _arp_table(host: Device) -> list[str]:
    return [arp_line(n) for n in host.shell.neighbors()]


def _route_prefixes(router: Device) -> list[str]:
    return sorted(router.ospf.routes())
