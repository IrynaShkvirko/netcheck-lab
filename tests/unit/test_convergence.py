"""Reconvergence measurement on the healthy fake lab (ADR-0004)."""

from __future__ import annotations

import pytest

from netcheck.convergence import measure_reconvergence
from netcheck.timing import Timing


@pytest.fixture
def no_sleep(monkeypatch):  # type: ignore[no-untyped-def]
    import netcheck.convergence as convergence

    monkeypatch.setattr(convergence.time, "sleep", lambda _: None)


def test_measures_both_planes_and_records_only_the_control_plane(fake, no_sleep) -> None:  # type: ignore[no-untyped-def]
    from tests.unit.fakes import make_healthy

    lab, parts = fake
    make_healthy(lab, parts)  # r1 routes 10.0.30.0/24 via the primary until p2 goes down
    r1 = lab["r1"]
    result = measure_reconvergence(
        check="reconverge_local_link",
        src=lab["h1"],
        dst=lab["h4"],
        router=r1,
        prefix="10.0.30.0/24",
        expected_next_hop="10.0.12.3",
        action=lambda: r1.links.set_link("p2", up=False),
    )
    # the fake stream sends 300 and gets 290 back: 10 lost x 0.01 s (default interval)
    assert result.data_plane_s == 0.1
    assert result.control_plane_s >= 0
    (m,) = lab.timing.measurements
    assert (m.check, m.sla_s, m.breached) == ("reconverge_local_link_control_plane", None, False)


def test_observe_records_a_value_without_an_sla() -> None:
    timing = Timing("unit", {})
    m = timing.observe("anything_control_plane", 0.12345)
    assert (m.check, m.value_s, m.sla_s) == ("anything_control_plane", 0.123, None)
    with pytest.raises(KeyError):
        timing.record("typo_check", 1.0)  # an SLA check must exist: no silent skip
