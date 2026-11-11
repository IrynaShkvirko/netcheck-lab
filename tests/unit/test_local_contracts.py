"""Contract tests: the local parsers on real FRR and iproute2 output.

The samples in fixtures/local/ were recorded from a healthy campus-mini lab by `make
record-fixtures`, through the local backend's own adapters. Replaying them through the same
adapters must give exactly the state the intent implies, as the lab tests would see it. A
parser that misreads the real output fails here without a lab, and so does an adapter whose
command changed without new samples (it would ask for something never recorded).
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any, cast

import pytest

from netcheck.assertions.ospf import ospf_interface_drift
from netcheck.backends import provenance
from netcheck.backends.local import LinuxShell, LocalFactory
from netcheck.devices.assembly import DeviceSpec, assemble
from netcheck.devices.device import Device
from netcheck.devices.transport.base import CommandResult, CommandTransport
from netcheck.model.capabilities import Bridge, Links, Ospf
from netcheck.model.expected import Expected
from netcheck.model.intent import load_intent
from netcheck.run.local_samples import peers, ping

from .fakes_local import ScriptedTransport
from .intents import ROOT

SAMPLES = Path(__file__).resolve().parent / "fixtures" / "local"
INTENT = load_intent(ROOT / "intent" / "lab.yaml")
EXPECTED = Expected(INTENT)


def _replay() -> tuple[dict[str, Device], dict[str, ScriptedTransport]]:
    transports: dict[str, ScriptedTransport] = {}

    def open_transport(spec: DeviceSpec) -> CommandTransport:
        calls = json.loads((SAMPLES / f"{spec.name}.json").read_text())["calls"]
        replies: dict[str, list[Any]] = {}
        for c in calls:
            replies.setdefault(c["cmd"], []).append(
                CommandResult(c["cmd"], c["stdout"], c["stderr"], c["rc"], spec.name)
            )
        transports[spec.name] = ScriptedTransport(replies)
        return transports[spec.name]

    return assemble(INTENT, "local", "eth", LocalFactory(open_transport)), transports


DEVICES, TRANSPORTS = _replay()
pytestmark = [] if provenance.status(SAMPLES).verified else [pytest.mark.documentation_samples]


def _devices(kind: type | None = None, role: str = "") -> list[str]:
    return sorted(
        n
        for n, d in DEVICES.items()
        if (kind is None or d.has(kind)) and (not role or d.role == role)
    )


def test_samples_are_captured_from_this_intent() -> None:
    status = provenance.status(SAMPLES)
    assert status.verified, f"{status.line('local')}: run `make record-fixtures` on a lab"
    for name in INTENT.nodes:
        sample = json.loads((SAMPLES / f"{name}.json").read_text())
        assert sample["intent"] == INTENT.name, f"{name}: recorded from {sample['intent']}"


@pytest.mark.parametrize("name", _devices(Links))
def test_every_intended_port_is_up(name: str) -> None:
    dev = DEVICES[name]
    states = dev.links.link_states(list(dev.port_map))
    assert sorted(states) == sorted(dev.port_map), name
    down = [s.line() for s in states.values() if not (s.admin_up and s.oper_up)]
    assert not down, f"{name}: {down}"


@pytest.mark.parametrize("name", _devices(Ospf))
def test_frr_neighbors_are_the_intended_adjacencies(name: str) -> None:
    got = sorted((n.router_id, n.state, n.port) for n in DEVICES[name].ospf.neighbors())
    want = sorted(
        (a.router_id, "Full", a.local_port) for a in EXPECTED.adjacencies() if a.router == name
    )
    assert got == want


@pytest.mark.parametrize("name", _devices(Ospf))
def test_frr_routes_are_the_intended_routes_over_the_cheapest_path(name: str) -> None:
    routes = DEVICES[name].ospf.routes()
    assert sorted(routes) == sorted(EXPECTED.routes(name))
    for prefix, route in routes.items():
        assert route.installed, prefix
        hops = {h.address for h in EXPECTED.ospf_next_hops(name, prefix)}
        assert set(route.next_hops) == hops, prefix


OSPF_NETWORKS = {
    str(ipaddress.ip_interface(address).network)
    for router in EXPECTED.routers()
    for iface in EXPECTED.ospf_interfaces(router).values()
    for address in iface.addresses
}


@pytest.mark.parametrize("name", _devices(Ospf))
def test_frr_lsdb_knows_exactly_the_networks_the_intent_puts_into_ospf(name: str) -> None:
    assert DEVICES[name].ospf.lsdb_prefixes() == OSPF_NETWORKS


@pytest.mark.parametrize("name", _devices(Ospf))
def test_frr_running_config_parses_to_the_intended_interfaces(name: str) -> None:
    """The real `show running-config`, not the one model/render.py wrote: what FRR prints back
    (area spelling, defaults it adds or drops) must read as the intent."""
    live = DEVICES[name].ospf.interface_settings()
    for iface, want in EXPECTED.ospf_interfaces(name).items():
        assert ospf_interface_drift(f"{name}:{iface}", want, live.get(iface)) == []
    extra = sorted(i for i, s in live.items() if s.area is not None)
    assert extra == sorted(EXPECTED.ospf_interfaces(name)), "OSPF interfaces outside the intent"


@pytest.mark.parametrize("name", _devices(Bridge))
def test_bridge_vlans_are_the_intended_vlans(name: str) -> None:
    assert DEVICES[name].bridge.vlan_membership() == EXPECTED.vlan_membership(name)


@pytest.mark.parametrize("name", _devices(role="host"))
def test_host_routes_and_pings_read_as_the_intent(name: str) -> None:
    shell = cast(LinuxShell, DEVICES[name].shell)
    gateway = INTENT.nodes[name].gateway
    default = [r for r in shell.route_table() if r.get("dst") == "default"]
    assert [r.get("gateway") for r in default] == ([gateway] if gateway else [])
    for peer in peers(name, EXPECTED.reachable_pairs()):
        r = ping(shell, INTENT.host_ip(peer))
        assert r.ran and r.received == r.sent, f"{name} -> {peer}: {r}"
        # the outage measurement reads the elapsed time from this line of real output
        assert r.interval_s is not None and 0.15 < r.interval_s < 0.5, f"{name}: {r.raw}"
    for peer in peers(name, EXPECTED.isolated_pairs()):
        r = ping(shell, INTENT.host_ip(peer))
        if gateway:  # a route exists: the probe goes out and nothing answers
            assert r.ran and r.received == 0, f"{name} -> {peer}: {r}"
        else:  # no route at all: ping cannot send, and that must not read as a result
            assert not r.ran and r.error, f"{name} -> {peer}: {r}"


def test_adapters_ask_only_for_what_was_recorded() -> None:
    """Runs after the reads above: any command they sent that the samples lack fell through
    as a write, which means an adapter's command changed. Re-record."""
    assert {n: t.writes for n, t in TRANSPORTS.items() if t.writes} == {}
