"""Functional: VLAN isolation and reachability, parametrized from the intent."""

import pytest

from netcheck.assertions import (
    assert_reachable,
    check_no_replies,
    force_layer2_path,
    host_attachment,
    isolation_probe_count,
    send_isolation_probes,
    start_capture,
)
from netcheck.lab import Lab
from netcheck.steps import step

pytestmark = pytest.mark.functional


def test_hosts_in_different_vlans_are_isolated(lab: Lab, isolated_pair: list[str]) -> None:
    """Two hosts the intent keeps apart cannot reach each other, even at layer 2."""
    a_name, b_name = isolated_pair
    a, b = lab[a_name], lab[b_name]

    with step(f"Look up where {a_name} and {b_name} are attached in the intent"):
        switch_a, port_a, vlan_a = host_attachment(lab.intent, a_name)
        switch_b, port_b, vlan_b = host_attachment(lab.intent, b_name)
        # e.g. h1 on sw1:p1 in VLAN 10, h3 on sw1:p3 in VLAN 20

    with step(f"Force {a_name} and {b_name} to talk directly over layer 2"):
        # on-link routes + empty ARP caches: a reply could only come from the same VLAN
        force_layer2_path(a, b)

    with step(f"Start a packet capture on {b_name}"):
        capture = start_capture(lab, b, "p1", "icmp or arp")

    with step(f"Send {isolation_probe_count()} pings from {a_name} to {b_name}"):
        result = send_isolation_probes(a, b)

    with step(f"Check {b_name} never answered ({result.received}/{result.sent} replies)"):
        check_no_replies(a, b, result, capture)


def test_hosts_reach_each_other(lab: Lab, reachable_pair: list[str]) -> None:
    """Two hosts the intent connects can ping each other."""
    a_name, b_name = reachable_pair

    with step(f"Look up {b_name}'s address in the intent"):
        address = lab.intent.host_ip(b_name)

    timeout = lab.timing["reachability"].timeout
    with step(f"Ping {address} from {a_name} until {b_name} answers (up to {timeout:.0f}s)"):
        assert_reachable(lab[a_name], lab[b_name])
