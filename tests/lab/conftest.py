"""Lab test parametrization: every case comes from the intent file (data-driven)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from netcheck.device import Device
from netcheck.intent import expected_state, load_intent
from netcheck.lab import Lab


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    intent = load_intent(metafunc.config.getoption("--intent"))
    expected = expected_state(intent)
    if "isolated_pair" in metafunc.fixturenames:
        pairs = expected["isolated"]
        metafunc.parametrize("isolated_pair", pairs, ids=[f"{a}-{b}" for a, b in pairs])
    if "reachable_pair" in metafunc.fixturenames:
        pairs = expected["reachable"]
        metafunc.parametrize("reachable_pair", pairs, ids=[f"{a}-{b}" for a, b in pairs])
    if "ospf_adjacency" in metafunc.fixturenames:
        adj = [(r, n["neighbor"]) for r, ns in expected["ospf_neighbors"].items() for n in ns]
        unique = sorted(set(adj))
        metafunc.parametrize("ospf_adjacency", unique, ids=[f"{a}-{b}" for a, b in unique])
    if "ospf_route" in metafunc.fixturenames:
        routes = [(r, p) for r, ps in expected["routes"].items() for p in ps]
        metafunc.parametrize("ospf_route", routes, ids=[f"{r}-{p}" for r, p in routes])
    if "switch_name" in metafunc.fixturenames:
        switches = sorted(expected["vlan_membership"])
        metafunc.parametrize("switch_name", switches)


@pytest.fixture
def node(lab: Lab) -> Callable[[str], Device]:
    """node("h1") -> Device"""
    return lab.__getitem__
