"""NVIDIA backend, device side: NVUE responses -> normalized state, and config changes
through the NVUE revision flow. Runs on the JSON samples in fixtures/nvue/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from netcheck.backends.local import parse_frr_interfaces
from netcheck.backends.nvidia_air import (
    NvueBridge,
    NvueLinks,
    NvueOspf,
    NvueShapeError,
    apply_changes,
    parse_nvue_bridge,
    parse_nvue_neighbors,
    parse_nvue_routes,
)
from netcheck.capabilities import OspfNeighbor, PortVlans, Route
from netcheck.intent import Intent, expected_state
from netcheck.render import frr_config
from netcheck.transport.base import TransportError

from .fakes_nvue import SAMPLES, FakeNvueTransport, nvue_ports


def _sample(device: str, name: str) -> Any:
    return json.loads((SAMPLES / device / f"{name}.json").read_text())


def _ospf(intent: Intent, device: str) -> tuple[NvueOspf, FakeNvueTransport]:
    t = FakeNvueTransport(device)
    return NvueOspf(t, nvue_ports(intent, device)), t  # type: ignore[arg-type]


# ---- parsers: the same state the local backend sees on a healthy lab -----------------


def test_ospf_interface_settings_match_the_frr_config_of_the_same_intent(intent) -> None:  # type: ignore[no-untyped-def]
    """Cross-backend contract: the NVUE parser and the FRR parser must normalize the
    same intent to identical OspfInterface values, or the lab tests would behave
    differently per backend."""
    for router in ("r1", "r2"):
        ospf, _ = _ospf(intent, router)
        ports = nvue_ports(intent, router)
        from_frr = parse_frr_interfaces(
            frr_config(intent, router, "swp"), {v: k for k, v in ports.items()}
        )
        assert ospf.interface_settings() == from_frr, router


def test_neighbors_are_per_link_with_frr_spelling(intent) -> None:  # type: ignore[no-untyped-def]
    ospf, _ = _ospf(intent, "r1")
    assert ospf.neighbors() == [
        OspfNeighbor("2.2.2.2", "Full", "p2"),
        OspfNeighbor("2.2.2.2", "Full", "p3"),
    ]


def test_routes_keep_only_what_ospf_won(intent) -> None:  # type: ignore[no-untyped-def]
    """r1's RIB also has connected routes, and OSPF entries that lost to them; only
    the OSPF-selected route is returned, like `show ip route ospf` on FRR."""
    ospf, _ = _ospf(intent, "r1")
    assert ospf.routes() == {"10.0.30.0/24": Route("10.0.30.0/24", "ospf", ("10.0.12.1",), True)}
    every = parse_nvue_routes(_sample("r1", "routes.operational"))
    assert {p: r.protocol for p, r in every.items()} == {
        "10.0.10.0/24": "connected",
        "10.0.12.0/31": "connected",
        "10.0.12.2/31": "connected",
        "10.0.30.0/24": "ospf",
    }


def test_routes_match_the_intent_on_both_routers(intent) -> None:  # type: ignore[no-untyped-def]
    for router, prefixes in expected_state(intent)["routes"].items():
        ospf, _ = _ospf(intent, router)
        assert sorted(ospf.routes()) == prefixes


def test_bridge_membership_matches_the_intent(intent) -> None:  # type: ignore[no-untyped-def]
    t = FakeNvueTransport("sw1")
    bridge = NvueBridge(t, nvue_ports(intent, "sw1"))  # type: ignore[arg-type]
    expected = {
        p: PortVlans(v["mode"], tuple(v["vlans"]))
        for p, v in expected_state(intent)["vlan_membership"]["sw1"].items()
    }
    assert bridge.vlan_membership() == expected  # br_default and eth0 are not ports


def test_bridge_port_defaults_and_unplanned_ports() -> None:
    interfaces = {
        "swp4": {"bridge": {"domain": {"br_default": {}}}},  # no list: every domain VLAN
        "swp5": {"bridge": {"domain": {"br_default": {"vlan": "all"}}}},
        "swp6": {"bridge": {"domain": {"br_default": {"vlan": {"20-22": {}}}}}},
        "swp9": {"bridge": {"domain": {"br_default": {"access": 10}}}},  # not in the intent
        "swp7": {"link": {}},  # not in the bridge
    }
    out = parse_nvue_bridge(
        interfaces, {"vlan": {"10": {}, "20": {}}}, {"swp4": "p4", "swp5": "p5", "swp6": "p6"}
    )
    assert out == {
        "p4": PortVlans("trunk", (10, 20)),
        "p5": PortVlans("trunk", (10, 20)),
        "p6": PortVlans("trunk", (20, 21, 22)),
        "swp9": PortVlans("access", (10,)),
    }


def test_link_states_read_every_port_in_one_call(intent) -> None:  # type: ignore[no-untyped-def]
    t = FakeNvueTransport("r1")
    links = NvueLinks(t, nvue_ports(intent, "r1"))  # type: ignore[arg-type]
    states = links.link_states(["p1", "p2", "p3"])
    assert {p: (s["ifname"], s["admin"], s["operstate"]) for p, s in states.items()} == {
        "p1": ("swp1", "up", "UP"),
        "p2": ("swp2", "up", "UP"),
        "p3": ("swp3", "up", "UP"),
    }
    assert [c[:2] for c in t.calls] == [("GET", "interface")]
    assert links.link_state("p2")["operstate"] == "UP"


@pytest.mark.parametrize(
    ("parser", "body"),
    [
        (lambda b: parse_nvue_neighbors(b, {}), ["not", "a", "dict"]),
        (lambda b: parse_nvue_neighbors(b, {}), {"2.2.2.2": {"interface": {"swp2": {}}}}),
        (parse_nvue_routes, {"10.0.30.0/24": {"distance": 110, "nexthop-group-id": 7}}),
        (lambda b: parse_nvue_bridge(b, {}, {}), {"swp1": {"bridge": {"domain": "br_default"}}}),
    ],
    ids=["neighbors-list", "neighbor-without-local-ip", "routes-5.11-shape", "bridge-domain-str"],
)
def test_unknown_shapes_fail_loudly(parser, body) -> None:  # type: ignore[no-untyped-def]
    """A schema change must not look like 'no neighbors' or 'no routes'."""
    with pytest.raises(NvueShapeError):
        parser(body)


# ---- config changes: revision flow ---------------------------------------------------


def test_set_access_vlan_replaces_a_trunk_in_one_revision(intent) -> None:  # type: ignore[no-untyped-def]
    t = FakeNvueTransport("sw1")
    NvueBridge(t, nvue_ports(intent, "sw1")).set_access_vlan("p4", 10)  # type: ignore[arg-type]
    assert t.changes == [
        ("DELETE", "interface/swp4/bridge/domain/br_default/vlan", None),
        ("PATCH", "interface/swp4/bridge/domain/br_default", {"access": 10}),
    ]
    edits = [c for c in t.calls if c[0] in ("PATCH", "DELETE") and not c[1].startswith("revision")]
    assert [c[3] for c in edits] == [{"rev": "1"}, {"rev": "1"}]  # both in revision 1
    assert t.calls[-2][:3] == (
        "PATCH",
        "revision/1",
        {"state": "apply", "auto-prompt": {"ays": "ays_yes"}},
    )


def test_ospf_area_change_and_removal(intent) -> None:  # type: ignore[no-untyped-def]
    ospf, t = _ospf(intent, "r2")
    ospf.set_interface_area("p3", None)  # the missing-ospf-network demo fault
    ospf.set_interface_area("p1.10", 0)
    assert t.changes == [
        ("DELETE", "interface/swp3/router/ospf/area", None),
        ("PATCH", "interface/swp1.10/router/ospf", {"area": "0"}),
    ]


def test_link_down_is_a_revision(intent) -> None:  # type: ignore[no-untyped-def]
    t = FakeNvueTransport("r1")
    NvueLinks(t, nvue_ports(intent, "r1")).set_link("p2", False)  # type: ignore[arg-type]
    assert t.changes == [("PATCH", "interface/swp2/link", {"state": {"down": {}}})]


def test_failed_apply_raises() -> None:
    t = FakeNvueTransport("sw1")
    t.apply_state = "invalid"
    with pytest.raises(TransportError, match="failed to apply"):
        apply_changes(t, [("PATCH", "interface/swp1", {})], sleep=lambda _: None)  # type: ignore[arg-type]


def test_apply_that_never_finishes_times_out() -> None:
    t = FakeNvueTransport("sw1")
    t.apply_state = "applying"
    with pytest.raises(TransportError, match="not applied after"):
        apply_changes(t, [], timeout=0, sleep=lambda _: None)  # type: ignore[arg-type]


def test_rejected_change_raises_but_deleting_nothing_is_fine() -> None:
    t = FakeNvueTransport("sw1")
    t.status_for[("DELETE", "interface/swp1/x")] = 404  # nothing to delete: not an error
    assert apply_changes(t, [("DELETE", "interface/swp1/x", None)]) == "1"  # type: ignore[arg-type]
    t.status_for[("PATCH", "interface/swp1")] = 400
    with pytest.raises(TransportError, match="HTTP 400"):
        apply_changes(t, [("PATCH", "interface/swp1", {"bad": 1})])  # type: ignore[arg-type]


# ---- the real lab tests, on NVUE-shaped responses ------------------------------------

_NVUE_LAB = """
import sys
sys.path.insert(0, {repo!r})
from pathlib import Path
import pytest
from netcheck.intent import load_intent
from netcheck.lab import set_current
from tests.unit.fakes import build_fake_lab, make_healthy
from tests.unit.fakes_nvue import use_nvue_devices

@pytest.fixture(scope="session")
def lab(tmp_path_factory):
    the_lab, parts = build_fake_lab(load_intent({intent!r}),
                                    Path(str(tmp_path_factory.mktemp("art"))))
    make_healthy(the_lab, parts)
    use_nvue_devices(the_lab)
    token = set_current(the_lab)
    yield the_lab
    token.var.reset(token)
"""


def test_lab_tests_pass_unchanged_on_nvue_responses(pytester) -> None:  # type: ignore[no-untyped-def]
    """The point of the capability layer: smoke, OSPF and drift tests from tests/lab
    run as they are, with routers and the switch answering in NVUE JSON."""
    repo = Path(__file__).resolve().parents[2]
    lab_dir = repo / "tests" / "lab"
    pytester.makeconftest(
        (lab_dir / "conftest.py").read_text()
        + _NVUE_LAB.format(repo=str(repo), intent=str(repo / "intent/lab.yaml"))
    )
    for f in lab_dir.glob("test_*.py"):
        pytester.makepyfile(**{f.stem: f.read_text()})
    result = pytester.runpytest_inprocess(
        "-p",
        "netcheck.pytest_plugin",
        "--intent",
        str(repo / "intent/lab.yaml"),
        "-k",
        "smoke or ospf or drift",
    )
    result.assert_outcomes(passed=10)
