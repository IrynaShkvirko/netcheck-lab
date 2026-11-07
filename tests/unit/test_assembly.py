"""Logical ports and building every device from the intent, on each backend."""

from __future__ import annotations

import pytest

from netcheck.assembly import assemble
from netcheck.backends.local import LocalFactory, LocalSnapshot
from netcheck.backends.nvidia_air import NvueFactory
from netcheck.capabilities import Bridge, Links, Ospf, Shell, Snapshot
from netcheck.device import CapabilityMissing
from netcheck.ports import PortMap

from .fakes_local import ScriptedTransport
from .fakes_nvue import FakeNvueTransport


def test_port_map_translates_both_ways_with_vlan_sub_interfaces() -> None:
    ports = PortMap({"p1": "swp1", "p2": "swp2"})
    assert ports["p2"] == "swp2" and list(ports) == ["p1", "p2"]
    assert (ports.real("p1.10"), ports.logical("swp1.10")) == ("swp1.10", "p1.10")
    assert ports.logical("swp9") == "swp9"  # a port the intent does not know keeps its name


def test_port_map_for_node_uses_the_backend_prefix(intent) -> None:  # type: ignore[no-untyped-def]
    assert PortMap.for_node(intent, "r1", "swp") == {"p1": "swp1", "p2": "swp2", "p3": "swp3"}
    assert PortMap.for_node(intent, "r1", "eth")["p2"] == "eth2"


def test_local_backend_gives_each_role_its_capabilities(intent) -> None:  # type: ignore[no-untyped-def]
    devices = assemble(intent, "local", "eth", LocalFactory(lambda spec: ScriptedTransport()))
    assert set(devices) == set(intent.nodes)
    kinds = {name: set(d.capabilities) for name, d in devices.items()}
    assert kinds["r1"] == {Links, Ospf, Snapshot}
    assert kinds["sw1"] == {Links, Bridge, Shell, Snapshot}
    assert kinds["h1"] == {Links, Shell, Snapshot}
    snap = devices["h1"].snapshot
    assert isinstance(snap, LocalSnapshot) and snap.host is devices["h1"].shell
    switch_snap = devices["sw1"].snapshot
    assert isinstance(switch_snap, LocalSnapshot) and switch_snap.host is None  # hosts only


def test_nvidia_hosts_are_devices_whose_tests_skip(intent) -> None:  # type: ignore[no-untyped-def]
    opened: list[str] = []

    def open_transport(spec):  # type: ignore[no-untyped-def]
        opened.append(spec.name)
        return FakeNvueTransport(spec.name)

    devices = assemble(intent, "nvidia", "swp", NvueFactory(open_transport))
    assert set(devices) == set(intent.nodes)
    assert set(devices["r1"].capabilities) == {Links, Ospf}
    assert set(devices["sw1"].capabilities) == {Links, Bridge}
    assert devices["h1"].capabilities == {}
    assert devices["h1"].port_map == {"p1": "swp1"}
    with pytest.raises(CapabilityMissing, match="h1 on backend 'nvidia' has no Shell"):
        _ = devices["h1"].shell
    assert sorted(opened) == sorted(n for n, node in intent.nodes.items() if node.role != "host")


def test_fake_lab_is_built_by_the_same_assembly(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    assert set(lab.devices) == set(lab.intent.nodes)
    assert lab["r1"].port_map == {"p1": "eth1", "p2": "eth2", "p3": "eth3"}
    assert lab["r1"].ospf is parts["ospf_r1"] and lab["sw1"].bridge is parts["bridge"]
