"""Layer 2: VLAN isolation and VLAN membership.

The isolation check is split into the pieces a test runs as separate steps:

    force_layer2_path(a, b)            # on-link routes, empty ARP caches
    capture = start_capture(...)       # evidence on the receiving host
    result = send_isolation_probes(a, b)
    check_no_replies(a, b, result, capture)

`assert_isolated` runs all four in order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from netcheck.assertions.common import (
    arp_line,
    attempt,
    capture_dir,
    evidence,
    fail,
    finish_capture,
    start_capture,
)
from netcheck.capabilities import CaptureHandle, PingResult, PortVlans
from netcheck.device import Device
from netcheck.intent import Intent
from netcheck.lab import Lab, current
from netcheck.report import DiffLine, FailureReport

PROBE_INTERVAL = 0.2  # seconds between isolation probes


def host_attachment(intent: Intent, host: str) -> tuple[str, str, int | None]:
    """(switch, switch_port, vlan) a host is plugged into; ('', '', None) if routed."""
    for lk in intent.links_of(host):
        peer = lk.peer(host)
        if intent.nodes[peer.node].role == "switch":
            return peer.node, peer.port, lk.vlan
    return "", "", None


def describe_vlans(mode: str, vlans: list[int] | tuple[int, ...]) -> str:
    """'access VLAN 10', 'trunk VLAN 10,20', 'none, no VLAN'."""
    if mode == "none" or not vlans:
        return f"{mode}, no VLAN"
    return f"{mode} VLAN {','.join(map(str, vlans))}"


def vlan_diff(
    switch: str,
    expected: Mapping[str, Mapping[str, Any]],
    live: Mapping[str, PortVlans],
    ports: list[str],
) -> list[DiffLine]:
    """Intent vs live mode and VLANs for each port in `ports` (all of them, equal or not)."""
    lines = []
    for port in ports:
        exp, got = expected.get(port), live.get(port)
        exp_s = describe_vlans(exp["mode"], exp["vlans"]) if exp else "absent"
        got_s = describe_vlans(got.mode, got.vlans) if got else "absent"
        note = "not in the intent" if not exp and got else "not on the bridge" if not got else ""
        lines.append(DiffLine(f"{switch}:{port}", exp_s, got_s, note))
    return lines


def extra_ports(expected: Mapping[str, Any], live: Mapping[str, PortVlans]) -> list[str]:
    """Live bridge ports the intent does not list."""
    return sorted(set(live) - set(expected))


def vlan_ports(live: Mapping[str, PortVlans]) -> list[str]:
    """Live VLAN -> ports map, as evidence lines ('VLAN 10: p1, p2, p3, p4')."""
    by_vlan: dict[int, list[str]] = {}
    for port, pv in sorted(live.items()):
        for vid in pv.vlans:
            by_vlan.setdefault(vid, []).append(port)
    return [f"VLAN {vid}: {', '.join(ps)}" for vid, ps in sorted(by_vlan.items())]


def vlan_drift(lab: Lab, switch: str, ports: list[str]) -> tuple[list[DiffLine], list[str]]:
    """Read the switch, then intent-vs-live lines for `ports` plus the VLAN map."""
    expected = lab.expected["vlan_membership"].get(switch, {})
    live = lab[switch].bridge.vlan_membership()
    return vlan_diff(switch, expected, live, ports), vlan_ports(live)


# ---- isolation, in steps ---------------------------------------------------------


def _undo_route(host: Device, prefix: str) -> Callable[[], None]:
    return lambda: host.shell.del_host_route(prefix)


def force_layer2_path(a: Device, b: Device) -> None:
    """Make a reply possible only if a and b share a broadcast domain.

    Each host gets an on-link /32 route to the other, so it ARPs for the peer directly
    instead of sending to its gateway, and its ARP cache is emptied. Without this an
    isolation check could pass because of routing, not because of the VLANs.
    The routes are registered for cleanup the moment they are added.
    """
    lab = current()
    ip_a, ip_b = lab.intent.host_ip(a.name), lab.intent.host_ip(b.name)
    for host, peer_ip in ((a, ip_b), (b, ip_a)):
        host.shell.add_host_route(f"{peer_ip}/32")
        lab.cleanup.register(
            f"remove on-link route {peer_ip}/32 on {host.name}", _undo_route(host, f"{peer_ip}/32")
        )
        host.shell.flush_neighbors()


def isolation_probe_count() -> int:
    window = current().timing["isolation_window"].window or 4.0
    return max(1, int(window / PROBE_INTERVAL))


def send_isolation_probes(a: Device, b: Device) -> PingResult:
    """Ping b from a for the full isolation window. Never stops early.

    A probe that never left `a` makes the check inconclusive: that fails, it does not pass.
    """
    lab = current()
    window = lab.timing["isolation_window"].window or 4.0
    count = isolation_probe_count()
    result = a.shell.ping(
        lab.intent.host_ip(b.name), count=count, interval=PROBE_INTERVAL, deadline=window
    )
    if result.sent == 0:
        fail(
            FailureReport(
                verdict=f"isolation {a.name}-{b.name} inconclusive: no probes were sent",
                expected=f"{count} ICMP probes sent, 0 replies",
                actual="0 probes sent",
            )
        )
    return result


def check_no_replies(
    a: Device, b: Device, result: PingResult, capture: CaptureHandle | None = None
) -> None:
    """Pass if b never answered. Otherwise report the VLAN drift, a's ARP table and
    what the capture on b saw."""
    lab = current()
    if result.received == 0:
        if capture:
            b.shell.stop_capture(capture, str(capture_dir(lab)))
        return
    sw_a, port_a, vlan_a = host_attachment(lab.intent, a.name)
    sw_b, port_b, vlan_b = host_attachment(lab.intent, b.name)
    report = FailureReport(
        verdict=f"{a.name} (VLAN {vlan_a}) and {b.name} (VLAN {vlan_b}) must be isolated",
        expected=f"0/{result.sent} ICMP replies {a.name} -> {b.name} over L2",
        actual=f"{result.received}/{result.sent} replies",
    )
    for sw in sorted({s for s in (sw_a, sw_b) if s}):
        ports = [p for s, p in ((sw_a, port_a), (sw_b, port_b)) if s == sw]
        drift, vlans = attempt(
            report,
            f"{sw} VLAN membership",
            partial(vlan_drift, lab, sw, ports),
            (list[DiffLine](), list[str]()),
        )
        report.drift += drift
        if vlans:
            report.add_evidence(f"{sw} VLAN membership", vlans)
    evidence(report, f"{a.name} neighbor table", lambda: [arp_line(n) for n in a.shell.neighbors()])
    finish_capture(lab, b, capture, report)
    fail(report)


def assert_isolated(a: Device, b: Device) -> PingResult:
    """a and b must not share a broadcast domain (all isolation steps in one call)."""
    lab = current()
    force_layer2_path(a, b)
    capture = start_capture(lab, b, "p1", "icmp or arp")
    result = send_isolation_probes(a, b)
    check_no_replies(a, b, result, capture)
    return result


# ---- VLAN membership --------------------------------------------------------------


def check_vlan_membership(
    switch: str, expected: Mapping[str, Mapping[str, Any]], live: Mapping[str, PortVlans]
) -> None:
    """Every port in `expected` has exactly the intended mode and VLANs, and the switch has
    no bridge port the intent does not list."""
    ports = sorted(set(expected) | set(live))
    bad = [d for d in vlan_diff(switch, expected, live, ports) if d.expected != d.actual]
    if bad:
        report = FailureReport(
            verdict=f"{switch} VLAN membership must match the intent",
            expected="all ports as in intent",
            actual=f"{len(bad)} port(s) differ",
            drift=bad,
        )
        report.add_evidence(f"{switch} VLAN membership", vlan_ports(live))
        fail(report)


def check_port_vlans(
    switch: str,
    port: str,
    expected: Mapping[str, Mapping[str, Any]],
    live: Mapping[str, PortVlans],
) -> None:
    """One switch port has exactly the intended mode and VLANs. Made for one soft step
    per port, so a run lists every port that differs."""
    (line,) = vlan_diff(switch, expected, live, [port])
    if line.expected != line.actual:
        report = FailureReport(
            verdict=f"{switch}:{port} must be {line.expected}",
            expected=line.expected,
            actual=line.actual,
            drift=[line],
        )
        report.add_evidence(f"{switch} VLAN membership", vlan_ports(live))
        fail(report)


def check_no_extra_ports(
    switch: str, expected: Mapping[str, Mapping[str, Any]], live: Mapping[str, PortVlans]
) -> None:
    """The switch has no bridge port the intent does not list (an unplanned port could
    carry a VLAN somewhere it must not reach)."""
    extra = extra_ports(expected, live)
    if extra:
        report = FailureReport(
            verdict=f"{switch} must have no bridge ports outside the intent",
            expected=f"only {', '.join(sorted(expected))}",
            actual=f"also {', '.join(extra)}",
            drift=vlan_diff(switch, expected, live, extra),
        )
        report.add_evidence(f"{switch} VLAN membership", vlan_ports(live))
        fail(report)


def assert_vlan_membership(switch: Device) -> None:
    """Read the switch and check every port against the intent."""
    lab = current()
    expected = lab.expected["vlan_membership"].get(switch.name, {})
    check_vlan_membership(switch.name, expected, switch.bridge.vlan_membership())
