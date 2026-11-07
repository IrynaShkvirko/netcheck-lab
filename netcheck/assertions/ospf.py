"""OSPF: adjacencies, learned routes, and route drift against the intent."""

from __future__ import annotations

import ipaddress
from collections.abc import Collection, Mapping
from functools import partial

from netcheck.assertions.common import (
    attempt,
    evidence,
    fail,
    neighbor_line,
    ospf_neighbor_lines,
)
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


def ospf_interface_drift(
    where: str, want: OspfInterface, live: OspfInterface | None
) -> list[DiffLine]:
    """The fields of a live OSPF interface that differ from the intended one; mismatches
    only. Area is compared as a number (FRR may show 0 as '0.0.0.0'), addresses as a set."""
    if live is None:
        return [DiffLine(f"{where} OSPF interface", "configured", "absent")]

    def shown(value: object) -> str:
        return "not set" if value is None else str(value)

    drift = []
    if not same_area(live.area, int(want.area or 0)):
        got = "not in OSPF" if live.area is None else live.area
        drift.append(DiffLine(f"{where} OSPF area", shown(want.area), got))
    if set(live.addresses) != set(want.addresses):
        drift.append(
            DiffLine(
                f"{where} addresses",
                ", ".join(want.addresses) or "none",
                ", ".join(live.addresses) or "none",
            )
        )
    for what, w, g in (
        ("passive", want.passive, live.passive),
        ("hello interval", want.hello_interval, live.hello_interval),
        ("dead interval", want.dead_interval, live.dead_interval),
        ("network type", want.network, live.network),
        ("cost", want.cost, live.cost),
    ):
        if w != g:
            drift.append(DiffLine(f"{where} {what}", shown(w), shown(g)))
    return drift


def interface_drift(lab: Lab, router: Device, prefix: str) -> list[DiffLine]:
    """How the interfaces that should advertise `prefix` on `router` differ from the intent."""
    settings = router.ospf.interface_settings()
    return [
        line
        for want in lab.expected.advertising_interfaces(router.name, prefix)
        for line in ospf_interface_drift(
            f"{router.name}:{want.name} ({router.port_map.real(want.name)})",
            want,
            settings.get(want.name),
        )
    ]


def port_drift(lab: Lab, router: Device, port: str, live: OspfInterface | None) -> list[DiffLine]:
    """OSPF settings of one router interface that differ from the intent."""
    want = lab.expected.ospf_interfaces(router.name).get(port)
    if want is None:
        return []
    lk = lab.intent.link_at(router.name, port.partition(".")[0])
    label = f"{router.name}:{port}" + (f" ({lk.label})" if lk.label else "")
    return ospf_interface_drift(label, want, live)


def link_drift(lab: Lab, router: Device) -> list[DiffLine]:
    """OSPF settings on all links to other routers that differ from the intent."""
    settings = router.ospf.interface_settings()
    return [
        line
        for port in lab.expected.router_link_ports(router.name)
        for line in port_drift(lab, router, port, settings.get(port))
    ]


class _RouterLook:
    """One router's OSPF neighbors and LSDB, read at most once per failure report.

    Reading adds the evidence (neighbor lines; 'evidence unavailable' if the router
    did not answer). A failed read is kept as its error text, so a diagnosis can say
    the router was unreadable instead of guessing from an empty table.
    """

    def __init__(self, report: FailureReport, router: Device) -> None:
        self.report, self.router = report, router
        self._neighbors: list[OspfNeighbor] | None = None
        self._lsdb: set[str] | None = None
        self.neighbor_error = self.lsdb_error = ""
        self._read_neighbors = self._read_lsdb = False

    def neighbors(self) -> list[OspfNeighbor] | None:
        if not self._read_neighbors:
            self._read_neighbors = True
            name = self.router.name
            try:
                self._neighbors = self.router.ospf.neighbors()
            except Exception as exc:  # noqa: BLE001 - evidence is best effort
                self.neighbor_error = f"{type(exc).__name__}: {exc}"
                self.report.add_evidence(
                    "evidence unavailable", [f"{name} neighbors: {self.neighbor_error}"]
                )
            else:
                lines = [neighbor_line(n) for n in self._neighbors]
                self.report.add_evidence(f"{name} neighbors", lines or ["(none)"])
        return self._neighbors

    def has_full(self) -> bool | None:
        """Any Full adjacency; None if the neighbor table could not be read."""
        nbrs = self.neighbors()
        return None if nbrs is None else any(n.state == "Full" for n in nbrs)

    def lsdb(self) -> set[str] | None:
        if not self._read_lsdb:
            self._read_lsdb = True
            try:
                self._lsdb = self.router.ospf.lsdb_prefixes()
            except Exception as exc:  # noqa: BLE001 - evidence is best effort
                self.lsdb_error = f"{type(exc).__name__}: {exc}"
                self.report.add_evidence(
                    "evidence unavailable", [f"{self.router.name} LSDB: {self.lsdb_error}"]
                )
        return self._lsdb


def diagnose_missing_route(lab: Lab, look: _RouterLook, prefix: str) -> str:
    """Why `look.router` lacks `prefix`: unreadable, no adjacency, not advertised, or
    not installed. Adds the evidence (neighbors, LSDB) and the config drift that points
    at the cause to the report; returns the diagnosis line."""
    report, router = look.report, look.router.name
    has_full = look.has_full()
    lsdb = look.lsdb()
    if lsdb is not None:
        report.add_evidence(
            f"{router} LSDB", [f"{prefix} {'present' if prefix in lsdb else 'absent'}"]
        )
    if has_full is None:
        return f"{router}: could not read its OSPF neighbors ({look.neighbor_error})"
    if not has_full:
        report.drift += attempt(
            report,
            f"{router} OSPF link settings",
            partial(link_drift, lab, lab[router]),
            list[DiffLine](),
        )
        return f"{router}: no Full adjacency, so it learns nothing"
    if lsdb is None:
        return f"{router}: could not read its LSDB ({look.lsdb_error})"
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
        neighbors = _RouterLook(report, r1).neighbors() or []  # one read: evidence + diagnosis
        evidence(report, f"{r2.name} neighbors", partial(ospf_neighbor_lines, r2))
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
        diagnosis = diagnose_missing_route(lab, _RouterLook(report, router), prefix)
        report.add_evidence("diagnosis", [diagnosis])
        fail(report)
    lab.timing.record("route_installed", elapsed)
    assert route is not None
    return route


def assert_ospf_interface_matches_intent(router: Device, port: str) -> OspfInterface:
    """The OSPF settings on `router:port` are the intent's: area, hello and dead interval
    (neighbors only form when these match on both ends), network type, cost (it decides
    the path) and addresses."""
    lab = current()
    live = router.ospf.interface_settings().get(port)
    drift = port_drift(lab, router, port, live)
    if drift:
        report = FailureReport(
            verdict=f"{router.name}:{port} OSPF settings must match the intent",
            expected="OSPF area, timers, network type, cost and addresses as in the intent",
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
    looks = {r: _RouterLook(report, lab[r]) for r in routers}
    has_full = {r: looks[r].has_full() for r in routers}  # reads every router's neighbors
    diagnosis: list[str] = []
    # "no adjacency anywhere" only when every neighbor table was read and none is Full
    if missing and all(f is False for f in has_full.values()):
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
            diagnosis += [diagnose_missing_route(lab, looks[r], p) for p in ps]
    if extra:
        diagnosis.append(
            "unexpected routes: a router advertises a prefix the intent does not list "
            "(check its OSPF interfaces)"
        )
    report.add_evidence("diagnosis", diagnosis)
    return report
