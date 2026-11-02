"""Domain assertions (ADR-0002, ADR-0005).

Each assertion checks one intent and, when it fails, collects the evidence that
explains why before raising IntentViolation. Tests read like requirements:

    assert_isolated(h1, h3)
    assert_ospf_neighbor_full(r1, r2)
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path

from netcheck.capabilities import CaptureHandle, PingResult, Route
from netcheck.device import Device
from netcheck.intent import Intent, ospf_prefixes
from netcheck.lab import Lab, current
from netcheck.report import DiffLine, FailureReport, IntentViolation
from netcheck.timing import WaitTimeout

# ---- helpers ------------------------------------------------------------


def host_attachment(intent: Intent, host: str) -> tuple[str, str, int | None]:
    """(switch, switch_port, vlan) a host is plugged into; ('', '', None) if routed."""
    for lk in intent.links_of(host):
        peer = lk.peer(host)
        if intent.nodes[peer.node].role == "switch":
            return peer.node, peer.port, lk.vlan
    return "", "", None


def interface_blocks(running_config: str) -> dict[str, list[str]]:
    """Split FRR running-config into {interface: [lines]}."""
    blocks: dict[str, list[str]] = {}
    current_if: str | None = None
    for line in running_config.splitlines():
        m = re.match(r"^interface (\S+)", line)
        if m:
            current_if = m.group(1)
            blocks[current_if] = []
        elif line.startswith(("!", "exit")) or not line.startswith(" "):
            current_if = None
        elif current_if:
            blocks[current_if].append(line.strip())
    return blocks


def _vlan_drift(lab: Lab, switch: str, ports: list[str]) -> tuple[list[DiffLine], list[str]]:
    expected = lab.expected["vlan_membership"].get(switch, {})
    live = lab[switch].bridge.vlan_membership()
    drift, evidence = [], []
    for port in ports:
        exp = expected.get(port)
        got = live.get(port)
        exp_s = f"{exp['mode']} VLAN {','.join(map(str, exp['vlans']))}" if exp else "absent"
        got_s = f"{got.mode} VLAN {','.join(map(str, got.vlans))}" if got else "absent"
        drift.append(DiffLine(f"{switch}:{port}", exp_s, got_s))
    by_vlan: dict[int, list[str]] = {}
    for port, pv in sorted(live.items()):
        for vid in pv.vlans:
            by_vlan.setdefault(vid, []).append(port)
    evidence += [f"VLAN {vid}: {', '.join(ps)}" for vid, ps in sorted(by_vlan.items())]
    return drift, evidence


def _capture_dir(lab: Lab) -> Path:
    path = lab.test_artifacts
    path.mkdir(parents=True, exist_ok=True)
    return path


def _start_capture(lab: Lab, device: Device, port: str, bpf: str) -> CaptureHandle | None:
    try:
        handle = device.shell.start_capture(port, bpf)
    except Exception:  # capture is evidence, never the reason a test fails
        return None
    lab.cleanup.register(
        f"stop capture {device.name}:{port}",
        lambda: device.shell.stop_capture(handle, str(_capture_dir(lab))),
    )
    return handle


def _finish_capture(
    lab: Lab, device: Device, handle: CaptureHandle | None, report: FailureReport
) -> None:
    if handle is None:
        return
    try:
        done = device.shell.stop_capture(handle, str(_capture_dir(lab)))
        shown = done.summary[:CAPTURE_LINES]
        more = len(done.summary) - len(shown)
        report.capture_summary += [f"{device.name}:{handle.port}", *shown]
        if more > 0:
            report.capture_summary.append(f"... {more} more packets in the pcap")
        if done.local_path:
            report.capture_files.append(done.local_path)
    except Exception as exc:  # noqa: BLE001
        report.capture_summary.append(f"capture unavailable: {exc}")


def _undo_route(host: Device, prefix: str) -> Callable[[], None]:
    return lambda: host.shell.del_host_route(prefix)


CAPTURE_LINES = 6  # packets shown in the report; the full capture is in the pcap


def _neigh_line(n: Mapping[str, object]) -> str:
    state = n.get("state")
    state_s = ",".join(map(str, state)) if isinstance(state, list) else str(state)
    return f"{n.get('dst')} lladdr {n.get('lladdr')} {state_s}"


def _fail(report: FailureReport) -> None:
    raise IntentViolation(report)


# ---- reachability --------------------------------------------------------


def assert_reachable(a: Device, b: Device) -> PingResult:
    lab = current()
    dst = lab.intent.host_ip(b.name)
    th = lab.timing["route_installed"]
    try:
        result, _ = lab.timing.wait_until(
            "route_installed",
            lambda: a.shell.ping(dst, count=3, interval=0.2, deadline=3),
            lambda r: r.received > 0,
        )
        return result
    except WaitTimeout as exc:
        last = exc.last if isinstance(exc.last, PingResult) else None
        report = FailureReport(
            verdict=f"{a.name} must reach {b.name} ({dst})",
            expected=f"ICMP replies from {dst} within {th.timeout:.0f}s",
            actual=f"{last.received}/{last.sent} replies" if last else "no result",
        )
        report.add_evidence(f"{a.name} neighbor table", [str(n) for n in a.shell.neighbors()])
        for r in lab.by_role("router"):
            try:
                routes = r.ospf.routes()
                report.add_evidence(f"{r.name} OSPF routes", sorted(routes))
            except Exception:  # noqa: BLE001
                pass
        _fail(report)
        raise  # unreachable; keeps type checkers happy


def assert_isolated(a: Device, b: Device) -> PingResult:
    """a and b must not share a broadcast domain.

    Adds /32 on-link routes on both hosts (registered for cleanup), so a reply can
    only arrive if the two hosts are on the same L2 segment. Never passes early:
    the full window always runs.
    """
    lab = current()
    ip_a, ip_b = lab.intent.host_ip(a.name), lab.intent.host_ip(b.name)
    window = lab.timing["isolation_window"].window or 4.0
    count = max(1, int(window / 0.2))

    for host, peer_ip in ((a, ip_b), (b, ip_a)):
        host.shell.add_host_route(f"{peer_ip}/32")
        lab.cleanup.register(
            f"remove on-link route {peer_ip}/32 on {host.name}", _undo_route(host, f"{peer_ip}/32")
        )
        host.shell.flush_neighbors()

    capture = _start_capture(lab, b, "p1", "icmp or arp")
    result = a.shell.ping(ip_b, count=count, interval=0.2, deadline=window)
    if result.received == 0:
        if capture:
            b.shell.stop_capture(capture, str(_capture_dir(lab)))
        return result

    sw_a, port_a, vlan_a = host_attachment(lab.intent, a.name)
    sw_b, port_b, vlan_b = host_attachment(lab.intent, b.name)
    report = FailureReport(
        verdict=(f"{a.name} (VLAN {vlan_a}) and {b.name} (VLAN {vlan_b}) must be isolated"),
        expected=f"0/{result.sent} ICMP replies {a.name} -> {b.name} over L2",
        actual=f"{result.received}/{result.sent} replies",
    )
    for sw in sorted({s for s in (sw_a, sw_b) if s}):
        ports = [p for s, p in ((sw_a, port_a), (sw_b, port_b)) if s == sw]
        drift, evidence = _vlan_drift(lab, sw, ports)
        report.drift += drift
        report.add_evidence(f"{sw} VLAN membership", evidence)
    report.add_evidence(
        f"{a.name} neighbor table",
        [_neigh_line(n) for n in a.shell.neighbors()],
    )
    _finish_capture(lab, b, capture, report)
    _fail(report)
    return result


# ---- OSPF --------------------------------------------------------------


def assert_ospf_neighbor_full(r1: Device, r2: Device, port: str | None = None) -> float:
    """r1 sees r2 in state Full on its local `port`. Returns seconds waited.

    With two links between the same routers there are two adjacencies; `port` picks
    one. Matching on the router ID alone would pass while one of them is down.
    """
    lab = current()
    rid = lab.intent.nodes[r2.name].router_id or ""

    def state() -> str:
        for n in r1.ospf.neighbors():
            if n.router_id == rid and (port is None or n.port == port):
                return n.state
        return "absent"

    try:
        _, elapsed = lab.timing.wait_until("ospf_neighbor_full", state, lambda s: s == "Full")
    except WaitTimeout as exc:
        via = f" via {r1.name}:{port}" if port else ""
        report = FailureReport(
            verdict=f"{r1.name} must have a Full OSPF adjacency with {r2.name} ({rid}){via}",
            expected="state Full",
            actual=f"state {exc.last}",
        )
        for r in (r1, r2):
            report.add_evidence(
                f"{r.name} neighbors",
                [f"{n.router_id} {n.state} via {n.port}" for n in r.ospf.neighbors()] or ["(none)"],
            )
        others = [n.port for n in r1.ospf.neighbors() if n.router_id == rid and n.state == "Full"]
        if port and others:
            report.add_evidence(
                "diagnosis",
                [f"{r1.name} is Full with {r2.name} via {', '.join(others)}; only {port} is down"],
            )
        for lk in lab.intent.links:
            if {lk.a.node, lk.b.node} != {r1.name, r2.name}:
                continue
            ep = lk.endpoint(r1.name)
            if port is None or ep.port == port:
                label = f" ({lk.label})" if lk.label else ""
                report.add_evidence(
                    f"{r1.name}:{ep.port} link{label}", [str(r1.links.link_state(ep.port))]
                )
        _fail(report)
        raise
    lab.timing.record("ospf_neighbor_full", elapsed)
    return elapsed


def assert_route_present(router: Device, prefix: str) -> Route:
    """router has `prefix` installed. On failure, tells adjacency problems apart
    from advertisement problems (the discriminating evidence)."""
    lab = current()

    def lookup() -> Route | None:
        return router.ospf.routes().get(prefix)

    try:
        route, elapsed = lab.timing.wait_until(
            "route_installed", lookup, lambda r: r is not None and r.installed
        )
    except WaitTimeout:
        report = _route_missing_report(lab, router, prefix)
        _fail(report)
        raise
    lab.timing.record("route_installed", elapsed)
    assert route is not None
    return route


def _route_missing_report(lab: Lab, router: Device, prefix: str) -> FailureReport:
    originators = [
        r for r, ps in ospf_prefixes(lab.intent).items() if prefix in ps and r != router.name
    ]
    origin = originators[0] if originators else "?"
    report = FailureReport(
        verdict=f"{router.name} must learn {prefix} via OSPF from {origin}",
        expected=f"{prefix} installed in {router.name} RIB",
        actual="route absent",
    )
    neighbors = router.ospf.neighbors()
    all_full = bool(neighbors) and all(n.state == "Full" for n in neighbors)
    report.add_evidence(
        f"{router.name} neighbors",
        [f"{n.router_id} {n.state} via {n.port}" for n in neighbors] or ["(none)"],
    )
    in_lsdb = prefix in router.ospf.lsdb_prefixes()
    report.add_evidence(f"{router.name} LSDB", [f"{prefix} {'present' if in_lsdb else 'absent'}"])
    if all_full and not in_lsdb:
        report.add_evidence(
            "diagnosis",
            [
                "adjacencies are Full but the prefix is not in the LSDB:",
                f"{origin} is not advertising {prefix} (check its OSPF interface config)",
            ],
        )
    if origin in lab.devices:
        report.drift += _ospf_interface_drift(lab, lab[origin], prefix)
    return report


def _ospf_interface_drift(lab: Lab, router: Device, prefix: str) -> list[DiffLine]:
    """Compare the originator's interface OSPF config with the intent."""
    import ipaddress

    blocks = interface_blocks(router.ospf.running_config())
    area = lab.intent.ospf.area
    drift = []
    for lk in lab.intent.links_of(router.name):
        ep = lk.endpoint(router.name)
        if ep.ip and str(ipaddress.ip_interface(ep.ip).network) == prefix:
            real = router.port_map.get(ep.port, ep.port)
            lines = blocks.get(real, [])
            has_area = any(ln.startswith(f"ip ospf area {area}") for ln in lines)
            drift.append(
                DiffLine(
                    f"{router.name}:{ep.port} ({real})",
                    f"ip ospf area {area}",
                    f"ip ospf area {area}" if has_area else "not in OSPF",
                )
            )
    return drift


def assert_vlan_membership(switch: Device) -> None:
    """Every switch port carries exactly the VLANs in the intent (drift check)."""
    lab = current()
    expected = lab.expected["vlan_membership"].get(switch.name, {})
    drift, evidence = _vlan_drift(lab, switch.name, sorted(expected))
    bad = [d for d in drift if d.expected != d.actual]
    if bad:
        report = FailureReport(
            verdict=f"{switch.name} VLAN membership must match the intent",
            expected="all ports as in intent",
            actual=f"{len(bad)} port(s) differ",
            drift=bad,
        )
        report.add_evidence(f"{switch.name} VLAN membership", evidence)
        _fail(report)


__all__ = [
    "assert_isolated",
    "assert_ospf_neighbor_full",
    "assert_reachable",
    "assert_route_present",
    "assert_vlan_membership",
    "host_attachment",
    "interface_blocks",
]
