"""Parsers for structured device output (shapes taken from FRR and iproute2 JSON)."""

from netcheck.backends.local import (
    parse_bridge_vlans,
    parse_frr_neighbors,
    parse_frr_routes,
    parse_ping,
)
from netcheck.ports import PortMap

PORTS = PortMap({"p2": "eth2", "p3": "eth3", "p1": "eth1", "p4": "eth4"})


def test_frr_neighbors_both_key_styles() -> None:
    data = {
        "neighbors": {
            "2.2.2.2": [
                {"nbrState": "Full/-", "ifaceName": "eth2:10.0.12.0"},
                {"state": "Init/DROther", "ifaceName": "eth3:10.0.12.2"},
            ]
        }
    }
    nbrs = parse_frr_neighbors(data, PORTS)
    assert [(n.router_id, n.state, n.port) for n in nbrs] == [
        ("2.2.2.2", "Full", "p2"),
        ("2.2.2.2", "Init", "p3"),
    ]


def test_frr_routes_keep_active_next_hops() -> None:
    data = {
        "10.0.30.0/24": [
            {
                "protocol": "ospf",
                "selected": True,
                "installed": True,
                "nexthops": [
                    {"ip": "10.0.12.1", "active": True},
                    {"ip": "10.0.12.3", "active": False},
                ],
            }
        ]
    }
    route = parse_frr_routes(data)["10.0.30.0/24"]
    assert route.next_hops == ("10.0.12.1",) and route.installed


def test_bridge_vlans_access_and_trunk() -> None:
    data = [
        {"ifname": "br0", "vlans": [{"vlan": 1}]},
        {"ifname": "eth1", "vlans": [{"vlan": 10, "flags": ["PVID", "Egress Untagged"]}]},
        {"ifname": "eth4", "vlans": [{"vlan": 10}, {"vlan": 20}]},
    ]
    out = parse_bridge_vlans(data, PORTS)
    assert out["p1"].mode == "access" and out["p1"].vlans == (10,)
    assert out["p4"].mode == "trunk" and out["p4"].vlans == (10, 20)
    assert "br0" not in out


def test_ping_iputils_and_busybox() -> None:
    assert (
        parse_ping("h1", "x", "5 packets transmitted, 0 received, 100% packet loss").received == 0
    )
    r = parse_ping("h1", "x", "3 packets transmitted, 3 packets received, 0% packet loss")
    assert (r.sent, r.received) == (3, 3)
