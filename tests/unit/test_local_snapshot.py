"""The local backend's Snapshot adapter, driven through a scripted transport (ADR-0003).

Each command gets canned replies, used in order (the last one repeats). Commands with
no reply are changes to the device: they succeed and are recorded in `writes`.
"""

from __future__ import annotations

from typing import Any

import pytest

from netcheck.backends.local import FrrOspf, LinuxBridge, LinuxLinks, LinuxShell, LocalSnapshot
from netcheck.ports import PortMap
from netcheck.transport.base import TransportError

from .fakes_local import ScriptedTransport

PORTS = PortMap({"p1": "eth1", "p2": "eth2", "p3": "eth3", "p4": "eth4"})

R1 = """Building configuration...
!
frr version 10.1_git
hostname r1
!
interface eth3
 ip address 10.0.30.1/24
 ip ospf area 0
 ip ospf passive
exit
!
router ospf
 ospf router-id 2.2.2.2
exit
!
end
"""

LINKS_UP = [{"ifname": f"eth{i}", "flags": ["UP"], "operstate": "UP"} for i in range(1, 5)]


def _links(down: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [{**ln, "flags": [] if ln["ifname"] in down else ["UP"]} for ln in LINKS_UP]


def router(running_configs: list[str], links: list[Any] | None = None):  # type: ignore[no-untyped-def]
    t = ScriptedTransport(
        {
            "ip -j link show": links or [LINKS_UP],
            "vtysh -c 'show running-config'": running_configs,
        }
    )
    snap = LocalSnapshot(
        "r1",
        LinuxLinks(t, PORTS),
        ospf=FrrOspf(t, PORTS),
    )
    return snap, t


def test_unchanged_router_sends_no_commands() -> None:
    snap, t = router([R1])
    snap.restore_to(snap.take())
    assert t.writes == []


def test_changed_frr_section_sends_only_the_differing_lines() -> None:
    faulted = R1.replace(" ip ospf area 0\n", " ip ospf cost 50\n")
    snap, t = router([R1, faulted, R1])
    state = snap.take()
    snap.restore_to(state)
    assert t.writes == [
        "vtysh -c 'conf t' -c 'interface eth3' -c 'no ip ospf cost 50' -c 'ip ospf area 0'"
    ]


def test_frr_still_differing_after_apply_raises() -> None:
    faulted = R1.replace(" ip ospf area 0\n", "")
    snap, _ = router([R1, faulted, faulted])
    state = snap.take()
    with pytest.raises(TransportError, match="r1: config still differs after restore"):
        snap.restore_to(state)


def test_link_taken_down_by_a_test_is_brought_back_up() -> None:
    snap, t = router([R1], links=[LINKS_UP, _links(down=("eth2",)), LINKS_UP])
    state = snap.take()
    snap.restore_to(state)
    assert t.writes == ["ip link set eth2 up"]


def test_link_still_down_after_restore_raises() -> None:
    """A link that does not come back must fail the restore, not leave the lab broken."""
    snap, _ = router([R1], links=[LINKS_UP, _links(down=("eth2",))])  # stays down
    state = snap.take()
    with pytest.raises(
        TransportError, match=r"r1: links still wrong after restore: p2 \(want up\)"
    ):
        snap.restore_to(state)


def test_missing_port_raises_instead_of_a_key_error() -> None:
    gone = [ln for ln in LINKS_UP if ln["ifname"] != "eth2"]
    snap, _ = router([R1], links=[LINKS_UP, gone])
    state = snap.take()
    with pytest.raises(TransportError, match="r1: port p2 is gone"):
        snap.restore_to(state)


def _bridge_json(p4: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"ifname": "br0", "vlans": [{"vlan": 1}]},
        {"ifname": "eth1", "vlans": [{"vlan": 10, "flags": ["PVID", "Egress Untagged"]}]},
        {"ifname": "eth4", "vlans": p4},
        {"ifname": "eth9", "vlans": [{"vlan": 10, "flags": ["PVID", "Egress Untagged"]}]},
    ]


TRUNK = [{"vlan": 10}, {"vlan": 20}]
ACCESS_99 = [{"vlan": 99, "flags": ["PVID", "Egress Untagged"]}]


def switch(bridge_replies: list[Any]):  # type: ignore[no-untyped-def]
    t = ScriptedTransport({"ip -j link show": [LINKS_UP], "bridge -j vlan show": bridge_replies})
    snap = LocalSnapshot(
        "sw1",
        LinuxLinks(t, PORTS),
        bridge=LinuxBridge(t, PORTS),
    )
    return snap, t


def test_trunk_port_changed_by_a_test_gets_its_tagged_vlans_back() -> None:
    snap, t = switch([_bridge_json(TRUNK), _bridge_json(ACCESS_99)])
    state = snap.take()
    snap.restore_to(state)
    assert t.writes == [
        "bridge vlan del dev eth4 vid 99",
        "bridge vlan add dev eth4 vid 10",
        "bridge vlan add dev eth4 vid 20",
    ]


def test_bridge_port_outside_the_intent_is_not_snapshotted() -> None:
    snap, _ = switch([_bridge_json(TRUNK)])
    assert "eth9" not in snap.take()["bridge"]


def test_host_routes_left_by_a_test_are_removed_and_default_restored() -> None:
    connected = {"dst": "10.0.10.0/24", "dev": "eth1", "protocol": "kernel", "scope": "link"}
    default = {"dst": "default", "gateway": "10.0.10.1", "dev": "eth1"}
    leftover = {"dst": "10.0.20.13", "dev": "eth1", "scope": "link"}
    t = ScriptedTransport(
        {
            "ip -j link show": [LINKS_UP],
            "ip -j route show": [[default, connected], [connected, leftover]],
        }
    )
    snap = LocalSnapshot(
        "h1",
        LinuxLinks(t, PortMap({"p1": "eth1"})),
        host=LinuxShell(t, PortMap({"p1": "eth1"}), "h1"),
    )
    state = snap.take()
    snap.restore_to(state)
    assert t.writes == [
        "ip route del 10.0.20.13 dev eth1",
        "ip route replace default via 10.0.10.1 dev eth1",
    ]
