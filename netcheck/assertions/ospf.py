"""OSPF: adjacencies, learned routes, and route drift against the intent."""

from __future__ import annotations

import ipaddress
from collections.abc import Collection, Mapping
from functools import partial

from netcheck.assertions.common import attempt, evidence, fail, ospf_neighbor_lines
from netcheck.capabilities import OspfInterface, OspfNeighbor, Route
from netcheck.device import Device
from netcheck.lab import Lab, current
from netcheck.report import DiffLine, FailureReport
from netcheck.timing import WaitTimeout

# ---- comparing interface settings with the intent ------------------------------


def same_area(live: str | None, wanted: int) -> bool:
    """FRR may show area 0 as '0' or '0.0.0.0'."""
    if live is None:
        return False
    try:
        return int(ipaddress.IPv4Address(live)) == wanted
    except ValueError:
        return live.isdigit() and int(live) == wanted


def interface_drift(lab: Lab, router: Device, prefix: str) -> list[DiffLine]:
    """Is each interface that should advertise `prefix` on `router` in OSPF?"""
    settings = router.ospf.interface_settings()
    area = lab.intent.ospf.area
    drift = []
    for want in lab.expected.advertising_interfaces(router.name, prefix):
        s = settings.get(want.name)
        real = router.port_map.real(want.name)
        in_area = f"OSPF area {s.area}" if s and s.area is not None else "not in OSPF"
        if want.vlan is None:
            drift.append(
                DiffLine(f"{router.name}:{want.name} ({real})", f"OSPF area {area}", in_area)
            )
            continue
        has_ip = bool(s and want.address in s.addresses)
        drift.append(
            DiffLine(
                f"{router.name}:{want.name} ({real}, VLAN {want.vlan} gateway)",
                f"address {want.address}, OSPF area {area}",
                ", ".join(
                    [
                        f"address {want.address}" if has_ip else "no gateway address",
                        f"OSPF area {area}" if s and same_area(s.area, area) else "not in OSPF",
                    ]
                ),
            )
        )
    return drift


def port_drift(lab: Lab, router: Device, port: str, live: OspfInterface | None) -> list[DiffLine]:
    """Area, hello and dead interval of one router link vs the intent; mismatches only."""
    o = lab.intent.ospf
    s = live or OspfInterface(port)
    lk = lab.intent.link_at(router.name, port)
    label = f"{router.name}:{port}" + (f" ({lk.label})" if lk.label else "")
    drift = []
    if not same_area(s.area, o.area):
        drift.append(DiffLine(f"{label} OSPF area", str(o.area), s.area or "not set"))
    for what, want, got in (
        ("hello interval", o.hello_interval, s.hello_interval),
        ("dead interval", o.dead_interval, s.dead_interval),
    ):
        if got != want:
            drift.append(
                DiffLine(f"{label} {what}", str(want), "not set" if got is None else str(got))
            )
    return drift


def link_drift(lab: Lab, router: Device) -> list[DiffLine]:
    """OSPF settings on all links to other routers that differ from the intent."""
    settings = router.ospf.interface_settings()
    return [
        line
        for port in lab.expected.router_link_ports(router.name)
        for line in port_drift(lab, router, port, settings.get(port))
    ]


def _diagnose(
    lab: Lab, report: FailureReport, router: str, prefix: str, has_full: bool, lsdb: set[str]
) -> str:
    """Why `router` lacks `prefix`: no adjacency, not advertised, or not installed.
    Adds the config drift that points at the cause."""
    if not has_full:
        report.drift += attempt(
            report,
            f"{router} OSPF link settings",
            partial(link_drift, lab, lab[router]),
            list[DiffLine](),
        )
        return f"{router}: no Full adjacency, so it learns nothing"
    origin = lab.expected.originator(prefix, exclude=router)
    if prefix not in lsdb:
        if origin in lab.devices:
            report.drift += attempt(
                report,
                f"{origin} OSPF interface settings",
                partial(interface_drift, lab, lab[origin], prefix),
                list[DiffLine](),
            )
        return f"{router}: adjacency Full, but {origin} is not advertising {prefix}"
    return f"{router}: {prefix} is known to OSPF but not installed in the routing table"


def _link_line(router: Device, port: str) -> list[str]:
    return [router.links.link_state(port).line()]


# ---- assertions -----------------------------------------------------------------


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
            actual=exc.describe(f"state {exc.last}"),
        )
        for r in (r1, r2):
            evidence(report, f"{r.name} neighbors", partial(ospf_neighbor_lines, r))
        neighbors = attempt(report, f"{r1.name} neighbors", r1.ospf.neighbors, list[OspfNeighbor]())
        others = [n.port for n in neighbors if n.router_id == rid and n.state == "Full"]
        if port and others:
            report.add_evidence(
                "diagnosis",
                [f"{r1.name} is Full with {r2.name} via {', '.join(others)}; only {port} is down"],
            )
        for lk in lab.expected.links_between(r1.name, r2.name):
            ep = lk.endpoint(r1.name)
            if port is None or ep.port == port:
                label = f" ({lk.label})" if lk.label else ""
                evidence(
                    report, f"{r1.name}:{ep.port} link{label}", partial(_link_line, r1, ep.port)
                )
        fail(report)
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
    except WaitTimeout as exc:
        origin = lab.expected.originator(prefix, exclude=router.name)
        report = FailureReport(
            verdict=f"{router.name} must learn {prefix} via OSPF from {origin}",
            expected=f"{prefix} installed in {router.name} routing table",
            actual=exc.describe("route absent"),
        )
        neighbors = attempt(
            report, f"{router.name} neighbors", router.ospf.neighbors, list[OspfNeighbor]()
        )
        report.add_evidence(
            f"{router.name} neighbors",
            [f"{n.router_id} {n.state} via {n.port}" for n in neighbors] or ["(none)"],
        )
        lsdb = attempt(report, f"{router.name} LSDB", router.ospf.lsdb_prefixes, set[str]())
        report.add_evidence(
            f"{router.name} LSDB", [f"{prefix} {'present' if prefix in lsdb else 'absent'}"]
        )
        has_full = any(n.state == "Full" for n in neighbors)
        report.add_evidence(
            "diagnosis", [_diagnose(lab, report, router.name, prefix, has_full, lsdb)]
        )
        fail(report)
    lab.timing.record("route_installed", elapsed)
    assert route is not None
    return route


def assert_ospf_interface_matches_intent(router: Device, port: str) -> OspfInterface:
    """The OSPF settings on `router:port` (area, hello, dead interval) are the intent's.
    Two routers only become neighbors when these match on both ends."""
    lab = current()
    live = router.ospf.interface_settings().get(port)
    drift = port_drift(lab, router, port, live)
    if drift:
        report = FailureReport(
            verdict=f"{router.name}:{port} OSPF settings must match the intent",
            expected="OSPF area, hello and dead interval as in the intent",
            actual=f"{len(drift)} setting(s) differ",
            drift=drift,
        )
        report.add_evidence(f"{router.name}:{port} live OSPF settings", [str(live)])
        fail(report)
    assert live is not None
    return live


def check_next_hop(router: Device, route: Route, owner: str, allowed: set[str]) -> None:
    """`route` leaves `router` through `owner`: one of its next hops is an address of
    `owner` on a link the two share."""
    if set(route.next_hops) & allowed:
        return
    report = FailureReport(
        verdict=f"{router.name} must reach {route.prefix} through {owner}",
        expected=f"next hop in {sorted(allowed)}",
        actual=f"next hops {list(route.next_hops) or ['(none)']}",
    )
    evidence(report, f"{router.name} neighbors", partial(ospf_neighbor_lines, router))
    fail(report)


def assert_route_uses(router: Device, prefix: str, next_hop: str, why: str = "") -> Route:
    """Wait until `router` routes `prefix` via `next_hop`. `why` prefixes the verdict,
    e.g. 'before the failure, '."""
    lab = current()

    def lookup() -> Route | None:
        return router.ospf.routes().get(prefix)

    try:
        route, _ = lab.timing.wait_until(
            "route_installed",
            lookup,
            lambda r: r is not None and r.installed and next_hop in r.next_hops,
        )
    except WaitTimeout as exc:
        last = exc.last if isinstance(exc.last, Route) else None
        report = FailureReport(
            verdict=f"{why}{router.name} must route {prefix} via {next_hop}",
            expected=f"next hop {next_hop}",
            actual=exc.describe(f"next hops {list(last.next_hops)}" if last else "route absent"),
        )
        evidence(report, f"{router.name} neighbors", partial(ospf_neighbor_lines, router))
        fail(report)
    assert route is not None
    return route


def assert_ospf_routes_match_intent(expected: Mapping[str, Collection[str]] | None = None) -> None:
    """Every router has exactly the OSPF routes the intent expects: none missing,
    none unexpected.

    Polls like the other OSPF checks, so it does not fail while OSPF is still
    converging after a deploy. On failure it tells the causes apart: no adjacency
    at all, a router that is not advertising a prefix, or a route OSPF knows but
    did not install.
    """
    lab = current()
    wanted = expected if expected is not None else lab.expected.routes_by_router()

    def drift() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        missing: dict[str, list[str]] = {}
        extra: dict[str, list[str]] = {}
        for r, prefixes in wanted.items():
            live = {p for p, rt in lab[r].ospf.routes().items() if rt.installed}
            if gone := sorted(set(prefixes) - live):
                missing[r] = gone
            if unexpected := sorted(live - set(prefixes)):
                extra[r] = unexpected
        return missing, extra

    try:
        _, elapsed = lab.timing.wait_until(
            "route_installed", drift, lambda d: not d[0] and not d[1]
        )
    except WaitTimeout as exc:
        missing, extra = exc.last if isinstance(exc.last, tuple) else ({}, {})
        report = _routes_drift_report(lab, missing, extra)
        if exc.error is not None:
            report.add_evidence("last read failed", [str(exc.error)])
        fail(report)
    lab.timing.record("route_installed", elapsed)


def _routes_drift_report(
    lab: Lab, missing: dict[str, list[str]], extra: dict[str, list[str]]
) -> FailureReport:
    routers = lab.expected.routers()
    total = sum(len(ps) for ps in lab.expected.routes_by_router().values())
    n_missing = sum(len(ps) for ps in missing.values())
    n_extra = sum(len(ps) for ps in extra.values())
    report = FailureReport(
        verdict="every router must have exactly the OSPF routes the intent expects",
        expected=f"{total} routes installed, no others",
        actual=f"{n_missing} missing, {n_extra} unexpected",
        state_diff=[
            DiffLine(
                f"{r} routing table",
                p,
                "absent",
                note=f"advertised by {lab.expected.originator(p, r)}",
            )
            for r, ps in sorted(missing.items())
            for p in ps
        ]
        + [
            DiffLine(f"{r} routing table", "absent", p, note="not in the intent")
            for r, ps in sorted(extra.items())
            for p in ps
        ],
    )
    neighbors = {
        r: attempt(report, f"{r} neighbors", lab[r].ospf.neighbors, list[OspfNeighbor]())
        for r in routers
    }
    for r in routers:
        report.add_evidence(
            f"{r} neighbors",
            [f"{n.router_id} {n.state} via {n.port}" for n in neighbors[r]] or ["(none)"],
        )
    has_full = {r: any(n.state == "Full" for n in neighbors[r]) for r in routers}
    diagnosis: list[str] = []
    if missing and not any(has_full.values()):
        diagnosis.append(
            "no OSPF adjacency between the routers on any link: they exchange no routes"
        )
        for r in routers:
            report.drift += attempt(
                report,
                f"{r} OSPF link settings",
                partial(link_drift, lab, lab[r]),
                list[DiffLine](),
            )
        if not report.drift:
            diagnosis.append("OSPF link settings match the intent: check link state (make smoke)")
    else:
        for r, ps in sorted(missing.items()):
            lsdb = attempt(report, f"{r} LSDB", lab[r].ospf.lsdb_prefixes, set[str]())
            diagnosis += [_diagnose(lab, report, r, p, has_full[r], lsdb) for p in ps]
    if extra:
        diagnosis.append(
            "unexpected routes: a router advertises a prefix the intent does not list "
            "(check its OSPF interfaces)"
        )
    report.add_evidence("diagnosis", diagnosis)
    return report
