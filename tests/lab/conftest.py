"""Lab test parametrization: every case comes from the intent file (data-driven)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from netcheck.devices.device import Device
from netcheck.model.expected import Expected
from netcheck.model.intent import load_intent
from netcheck.run.lab import Lab


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    intent = load_intent(metafunc.config.getoption("--intent"))
    expected = Expected(intent)
    if "isolated_pair" in metafunc.fixturenames:
        pairs = expected.isolated_pairs()
        metafunc.parametrize("isolated_pair", pairs, ids=[f"{a}-{b}" for a, b in pairs])
    if "reachable_pair" in metafunc.fixturenames:
        pairs = expected.reachable_pairs()
        metafunc.parametrize("reachable_pair", pairs, ids=[f"{a}-{b}" for a, b in pairs])
    if "ospf_adjacency" in metafunc.fixturenames:
        # one case per adjacency: two links between the same routers are two cases
        adj = expected.adjacencies()
        metafunc.parametrize(
            "ospf_adjacency", adj, ids=[f"{a.router}-{a.neighbor}-{a.link}" for a in adj]
        )
    if "ospf_route" in metafunc.fixturenames:
        routes = [(r, p) for r in expected.routers() for p in sorted(expected.routes(r))]
        metafunc.parametrize("ospf_route", routes, ids=[f"{r}-{p}" for r, p in routes])
    if "switch_name" in metafunc.fixturenames:
        metafunc.parametrize("switch_name", expected.switches())


@pytest.fixture
def node(lab: Lab) -> Callable[[str], Device]:
    """node("h1") -> Device"""
    return lab.__getitem__
