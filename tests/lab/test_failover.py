"""Resilience: primary link failure, measured with traffic (ADR-0004)."""

import statistics

import pytest

from netcheck.assertions import assert_route_uses
from netcheck.convergence import measure_reconvergence
from netcheck.lab import Lab
from netcheck.steps import step

pytestmark = pytest.mark.resilience


def test_primary_link_failure_reroutes_via_backup(lab: Lab) -> None:
    """When the primary link fails, traffic moves to the backup within the SLA.

    With NETCHECK_REPEATS=N (the nightly job uses 5) the failure is run N times and the
    median outage is held to the SLA, so one noisy run on a shared machine neither fails
    nor passes the check on its own."""
    with step("Find the primary and backup links and a traffic path in the intent"):
        try:
            plan = lab.expected.failover()
        except LookupError as exc:
            pytest.skip(str(exc))
        # e.g. r1 fails over from 10.0.12.1 (primary) to 10.0.12.3 (backup); h1 -> h4
    router = lab[plan.router]
    check, runs = "reconverge_local_link", lab.timing.repeats  # NETCHECK_REPEATS, default 1

    def take_primary_down() -> None:
        router.links.set_link(plan.primary_port, up=False)
        lab.cleanup.register(
            f"bring {plan.router}:{plan.primary_port} back up",
            lambda: router.links.set_link(plan.primary_port, up=True),
        )

    outages = []
    for run in range(1, runs + 1):
        of = f" (run {run} of {runs})" if runs > 1 else ""
        with step(
            f"Check {plan.router} reaches {plan.prefix} via the primary ({plan.primary_hop}){of}"
        ):
            # starting on the wrong path would make the measurement meaningless; after a
            # run this also waits for the route to come back to the primary
            assert_route_uses(router, plan.prefix, plan.primary_hop, why="before the failure, ")

        with step(
            f"Send traffic {plan.src} -> {plan.dst}, take {plan.router}:{plan.primary_port} "
            f"down, wait for the route via {plan.backup_hop}{of}"
        ):
            result = measure_reconvergence(
                check=check,
                src=lab[plan.src],
                dst=lab[plan.dst],
                router=router,
                prefix=plan.prefix,
                expected_next_hop=plan.backup_hop,
                action=take_primary_down,
            )
            outages.append(result.data_plane_s)
            # every run is kept (no SLA): the history the SLA itself is derived from
            lab.timing.observe(f"{check}_run", result.data_plane_s)
            print(
                f"run {run}: data-plane outage {result.data_plane_s}s "
                f"(resolution {result.interval_s}s), control plane {result.control_plane_s}s, "
                f"{result.stream.loss} of {result.stream.sent} packets lost"
            )
        if run < runs:
            with step(f"Bring {plan.router}:{plan.primary_port} back up for the next run"):
                router.links.set_link(plan.primary_port, up=True)

    outage = statistics.median(outages)
    sla = lab.timing[check].sla
    measured = f"median of {runs} runs, {outage}s" if runs > 1 else f"{outage}s"
    with step(f"Check the outage ({measured}) is within the {sla:g}s SLA"):
        # fails in strict mode; records a warning with NETCHECK_SLA_MODE=record (PR CI)
        lab.timing.record(check, outage)
