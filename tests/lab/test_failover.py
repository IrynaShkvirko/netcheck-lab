"""Resilience: primary link failure, measured with traffic (ADR-0004)."""

import pytest

from netcheck.assertions import assert_route_uses
from netcheck.convergence import measure_reconvergence
from netcheck.lab import Lab
from netcheck.steps import step

pytestmark = pytest.mark.resilience


def test_primary_link_failure_reroutes_via_backup(lab: Lab) -> None:
    """When the primary link fails, traffic moves to the backup within the SLA."""
    with step("Find the primary and backup links and a traffic path in the intent"):
        try:
            plan = lab.expected.failover()
        except LookupError as exc:
            pytest.skip(str(exc))
        # e.g. r1 fails over from 10.0.12.1 (primary) to 10.0.12.3 (backup); h1 -> h4
    router = lab[plan.router]

    with step(f"Check {plan.router} reaches {plan.prefix} via the primary ({plan.primary_hop})"):
        # starting on the wrong path would make the measurement meaningless
        assert_route_uses(router, plan.prefix, plan.primary_hop, why="before the failure, ")

    def take_primary_down() -> None:
        router.links.set_link(plan.primary_port, up=False)
        lab.cleanup.register(
            f"bring {plan.router}:{plan.primary_port} back up",
            lambda: router.links.set_link(plan.primary_port, up=True),
        )

    with step(
        f"Send traffic {plan.src} -> {plan.dst}, take {plan.router}:{plan.primary_port} down, "
        f"wait for the route via {plan.backup_hop}"
    ):
        result = measure_reconvergence(
            check="reconverge_local_link",
            src=lab[plan.src],
            dst=lab[plan.dst],
            router=router,
            prefix=plan.prefix,
            expected_next_hop=plan.backup_hop,
            action=take_primary_down,
        )

    sla = lab.timing["reconverge_local_link"].sla
    with step(f"Check the outage ({result.data_plane_s}s) is within the {sla:g}s SLA"):
        # fails in strict mode; records a warning with NETCHECK_SLA_MODE=record (CI)
        lab.timing.record("reconverge_local_link", result.data_plane_s)
        print(
            f"data-plane outage {result.data_plane_s}s, control plane {result.control_plane_s}s, "
            f"{result.stream.loss} of {result.stream.sent} packets lost"
        )
