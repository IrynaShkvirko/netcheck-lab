"""Layer 2: VLAN isolation and VLAN membership.

The isolation check is split into the pieces a test runs as separate steps:

    check_not_routed(a, b)                # no router joins the VLANs
    force_layer2_path(a, b)               # on-link routes, empty ARP caches
    capture = start_capture(...)          # evidence on the receiving host
    result = send_isolation_probes(a, b)
    check_no_replies(a, b, result, capture)
    check_probes_reached_the_wire(a, b)   # a really sent ARP for b
    check_layer2_control(a, b, control_target(a))  # a reaches its own VLAN: a works

`assert_isolated` runs them all in order. A reply from b fails at once: isolation is
broken whatever else is wrong. Zero replies proves isolation only when a could have got
one, so the last two checks validate the silence: the probes left a, and a's link, port
and VLAN work (a reaches a host or gateway in its own VLAN over the same kind of path).
"""

from __future__ import annotations

import ipaddress
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

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
from netcheck.lab import Lab, current
from netcheck.report import DiffLine, FailureReport
from netcheck.timing import WaitTimeout

PROBE_INTERVAL = 0.2  # seconds between isolation probes


def describe_vlans(mode: str, vlans: list[int] | tuple[int, ...]) -> str:
    """'access VLAN 10', 'trunk VLAN 10,20', 'none, no VLAN'."""
    if mode == "none" or not vlans:
        return f"{mode}, no VLAN"
    return f"{mode} VLAN {','.join(map(str, vlans))}"


def vlan_diff(
    switch: str,
    expected: Mapping[str, PortVlans],
    live: Mapping[str, PortVlans],
    ports: list[str],
) -> list[DiffLine]:
    """Intent vs live mode and VLANs for each port in `ports` (all of them, equal or not)."""
    lines = []
    for port in ports:
        exp, got = expected.get(port), live.get(port)
        exp_s = describe_vlans(exp.mode, exp.vlans) if exp else "absent"
        got_s = describe_vlans(got.mode, got.vlans) if got else "absent"
        note = "not in the intent" if not exp and got else "not on the bridge" if not got else ""
        lines.append(DiffLine(f"{switch}:{port}", exp_s, got_s, note))
    return lines


def extra_ports(expected: Mapping[str, PortVlans], live: Mapping[str, PortVlans]) -> list[str]:
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
    expected = lab.expected.vlan_membership(switch)
    live = lab[switch].bridge.vlan_membership()
    return vlan_diff(switch, expected, live, ports), vlan_ports(live)


# ---- isolation, in steps ---------------------------------------------------------


def _undo_route(host: Device, prefix: str) -> Callable[[], None]:
    return lambda: host.shell.del_host_route(prefix)


def _force_on_link(host: Device, peer_ip: str) -> None:
    """An on-link /32 route from `host` to `peer_ip` (registered for cleanup at once) and
    an empty ARP cache: `host` must ARP for the peer itself."""
    lab = current()
    host.shell.add_host_route(f"{peer_ip}/32")
    lab.cleanup.register(
        f"remove on-link route {peer_ip}/32 on {host.name}", _undo_route(host, f"{peer_ip}/32")
    )
    host.shell.flush_neighbors()


def force_layer2_path(a: Device, b: Device) -> None:
    """Make a reply possible only if a and b share a broadcast domain.

    Each host gets an on-link /32 route to the other, so it ARPs for the peer directly
    instead of sending to its gateway, and its ARP cache is emptied. Without this an
    isolation check could pass because of routing, not because of the VLANs.
    The routes are registered for cleanup the moment they are added.
    """
    lab = current()
    _force_on_link(a, lab.intent.host_ip(b.name))
    _force_on_link(b, lab.intent.host_ip(a.name))


# ---- the controls around the isolation probe ----------------------------------------


@dataclass(frozen=True)
class ControlTarget:
    """Something in the sender's own VLAN to prove its path works: a host, or the VLAN's
    gateway when no other host shares the VLAN."""

    name: str  # "h2", or "r1 (VLAN 10 gateway)"
    ip: str
    host: str | None  # the host's name; None for a gateway


def control_target(a: Device) -> ControlTarget | None:
    """What in a's own VLAN proves a's path, depending on as little else as possible: a
    host on a's switch, else the VLAN's gateway, else a host on another switch of the VLAN,
    else None."""
    lab = current()
    at = lab.expected.attachment(a.name)
    if at is None:
        return None
    peers = lab.expected.broadcast_domain(a.name)
    local = [h for h in peers if (pa := lab.expected.attachment(h)) and pa.switch == at.switch]
    if local:
        return ControlTarget(local[0], lab.intent.host_ip(local[0]), local[0])
    gateway = lab.expected.vlan_gateway(a.name)
    if gateway:
        router, ip = gateway
        return ControlTarget(f"{router} (VLAN {at.vlan} gateway)", ip, None)
    if peers:
        return ControlTarget(peers[0], lab.intent.host_ip(peers[0]), peers[0])
    return None


def check_layer2_control(a: Device, b: Device, target: ControlTarget | None) -> None:
    """a reaches `target`, in its own VLAN, over the same kind of forced layer 2 path the
    isolation probe used. Without it, silence from b could mean a's own link, port or VLAN
    is broken, not that the VLANs keep a and b apart. Run after the probe got no reply:
    it flushes a's neighbor table. Retries until the `reachability` timeout.

    With no target (a is alone in a VLAN without a gateway) nothing can prove the path:
    that is a warning, not a failure, since the intent allows it.
    """
    lab = current()
    if target is None:
        warnings.warn(
            f"isolation {a.name}-{b.name}: no host or gateway shares {a.name}'s VLAN, so the "
            "path the probe takes cannot be proven",
            stacklevel=2,
        )
        return
    _force_on_link(a, target.ip)
    if target.host is not None:
        _force_on_link(lab[target.host], lab.intent.host_ip(a.name))
    try:
        lab.timing.wait_until(
            "reachability",
            lambda: a.shell.ping(target.ip, count=3, interval=PROBE_INTERVAL, deadline=3),
            lambda r: r.ran and r.received > 0,
        )
    except WaitTimeout as exc:
        last = exc.last if isinstance(exc.last, PingResult) else None
        if last is None:
            actual = exc.describe("no result")
        else:
            actual = last.error or f"{last.received}/{last.sent} replies"
        report = FailureReport(
            verdict=(
                f"isolation {a.name}-{b.name} inconclusive: {a.name} cannot reach "
                f"{target.name} ({target.ip}), in its own VLAN, over layer 2"
            ),
            expected=f"replies from {target.ip}: proof that {a.name}'s path works",
            actual=actual,
        )
        # the ports both ends hang off: the drift that explains a broken path
        ports: dict[str, list[str]] = {}
        for host in (a.name, target.host):
            at = lab.expected.attachment(host) if host else None
            if at is not None:
                ports.setdefault(at.switch, []).append(at.port)
        for switch, switch_ports in sorted(ports.items()):
            drift, vlans = attempt(
                report,
                f"{switch} VLAN membership",
                partial(vlan_drift, lab, switch, switch_ports),
                (list[DiffLine](), list[str]()),
            )
            report.drift += drift
            if vlans:
                report.add_evidence(f"{switch} VLAN membership", vlans)
        evidence(report, f"{a.name} link", lambda: [a.links.link_state("p1").line()])
        evidence(report, f"{a.name} neighbor table", partial(_arp_table, a))
        fail(report)


def check_not_routed(a: Device, b: Device) -> PingResult | None:
    """b does not answer a through a router either: no router may join the two VLANs.

    Probes b's address over a's normal routes for the full isolation window. Only when the
    intent gives a a gateway: without one a has no route to b and there is nothing to
    probe (returns None).
    """
    lab = current()
    if not lab.intent.nodes[a.name].gateway:
        return None
    result = _probe(a, b)
    if result.received == 0:
        return result
    report = FailureReport(
        verdict=f"{a.name} and {b.name} must be isolated, also through a router",
        expected=f"0/{result.sent} ICMP replies {a.name} -> {b.name} over {a.name}'s routes",
        actual=f"{result.received}/{result.sent} replies",
    )
    net = ipaddress.ip_interface(lab.intent.nodes[b.name].ip or "").network
    for router in lab.by_role("router"):
        evidence(report, f"{router.name} interfaces in {net}", partial(_interfaces_in, router, net))
    fail(report)


def check_probes_reached_the_wire(a: Device, b: Device) -> None:
    """After isolation probes that got no reply, a must have tried to resolve b by ARP:
    a neighbor entry for b (FAILED or INCOMPLETE). No entry means no probe left a, and
    silence from b proves nothing."""
    lab = current()
    ip_b = lab.intent.host_ip(b.name)
    if any(e.get("dst") == ip_b for e in a.shell.neighbors()):
        return
    report = FailureReport(
        verdict=(
            f"isolation {a.name}-{b.name} inconclusive: {a.name} never sent an ARP request "
            f"for {b.name} ({ip_b})"
        ),
        expected=f"a neighbor entry for {ip_b} on {a.name} (FAILED or INCOMPLETE)",
        actual="no entry: no probe reached the wire",
    )
    evidence(report, f"{a.name} neighbor table", partial(_arp_table, a))
    fail(report)


def _arp_table(host: Device) -> list[str]:
    return [arp_line(n) for n in host.shell.neighbors()]


def _interfaces_in(router: Device, net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> list[str]:
    return [
        f"{name} {address}"
        for name, s in sorted(router.ospf.interface_settings().items())
        for address in s.addresses
        if ipaddress.ip_interface(address).network.overlaps(net)
    ]


def isolation_probe_count() -> int:
    window = current().timing["isolation_window"].window or 4.0
    return max(1, int(window / PROBE_INTERVAL))


def send_isolation_probes(a: Device, b: Device) -> PingResult:
    """Ping b from a for the full isolation window. Never stops early.

    A ping that did not run makes the check inconclusive: that fails, it does not pass.
    """
    return _probe(a, b)


def _probe(a: Device, b: Device) -> PingResult:
    """Ping b's address from a for the isolation window; fail if no probe was sent."""
    lab = current()
    window = lab.timing["isolation_window"].window or 4.0
    count = isolation_probe_count()
    result = a.shell.ping(
        lab.intent.host_ip(b.name), count=count, interval=PROBE_INTERVAL, deadline=window
    )
    if not result.ran:
        fail(
            FailureReport(
                verdict=f"isolation {a.name}-{b.name} inconclusive: no probes were sent",
                expected=f"{count} ICMP probes sent, 0 replies",
                actual=result.error or "0 probes sent",
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
    at_a, at_b = lab.expected.attachment(a.name), lab.expected.attachment(b.name)
    vlan_a, vlan_b = (at.vlan if at else None for at in (at_a, at_b))
    report = FailureReport(
        verdict=f"{a.name} (VLAN {vlan_a}) and {b.name} (VLAN {vlan_b}) must be isolated",
        expected=f"0/{result.sent} ICMP replies {a.name} -> {b.name} over L2",
        actual=f"{result.received}/{result.sent} replies",
    )
    attached = [at for at in (at_a, at_b) if at is not None]
    for sw in sorted({at.switch for at in attached}):
        ports = [at.port for at in attached if at.switch == sw]
        drift, vlans = attempt(
            report,
            f"{sw} VLAN membership",
            partial(vlan_drift, lab, sw, ports),
            (list[DiffLine](), list[str]()),
        )
        report.drift += drift
        if vlans:
            report.add_evidence(f"{sw} VLAN membership", vlans)
    evidence(report, f"{a.name} neighbor table", partial(_arp_table, a))
    finish_capture(lab, b, capture, report)
    fail(report)


def assert_isolated(a: Device, b: Device) -> PingResult:
    """a and b must not reach each other, at layer 2 or through a router (all isolation
    steps in one call)."""
    lab = current()
    check_not_routed(a, b)
    force_layer2_path(a, b)
    capture = start_capture(lab, b, "p1", "icmp or arp")
    result = send_isolation_probes(a, b)
    check_no_replies(a, b, result, capture)
    check_probes_reached_the_wire(a, b)
    check_layer2_control(a, b, control_target(a))
    return result


# ---- VLAN membership --------------------------------------------------------------


def check_vlan_membership(
    switch: str, expected: Mapping[str, PortVlans], live: Mapping[str, PortVlans]
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
    expected: Mapping[str, PortVlans],
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
    switch: str, expected: Mapping[str, PortVlans], live: Mapping[str, PortVlans]
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
    expected = lab.expected.vlan_membership(switch.name)
    check_vlan_membership(switch.name, expected, switch.bridge.vlan_membership())
