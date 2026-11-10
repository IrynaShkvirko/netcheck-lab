"""Functional: VLAN isolation and reachability, parametrized from the intent."""

import pytest

from netcheck.assertions import (
    assert_reachable,
    check_layer2_control,
    check_no_replies,
    check_not_routed,
    check_probes_reached_the_wire,
    control_target,
    force_layer2_path,
    isolation_probe_count,
    send_isolation_probes,
    start_capture,
)
from netcheck.lab import Lab
from netcheck.steps import step

pytestmark = pytest.mark.functional


def test_hosts_in_different_vlans_are_isolated(lab: Lab, isolated_pair: tuple[str, str]) -> None:
    """Two hosts the intent keeps apart cannot reach each other, through a router or even
    at layer 2. Silence counts only once the sender's probes are shown to have left it and
    its own path is shown to work."""
    a_name, b_name = isolated_pair
    a, b = lab[a_name], lab[b_name]

    with step(f"Look up where {a_name} and {b_name} are attached in the intent"):
        attached_a = lab.expected.attachment(a_name)
        attached_b = lab.expected.attachment(b_name)
        # e.g. h1 on sw1:p1 in VLAN 10, h3 on sw1:p3 in VLAN 20
        assert attached_a and attached_b, f"{a_name} and {b_name} must both be on a switch"

    routed = lab.intent.nodes[a_name].gateway
    with step(
        f"Check {b_name} does not answer {a_name} through a router"
        if routed
        else f"{a_name} has no gateway in the intent: no routed path to probe"
    ):
        check_not_routed(a, b)

    with step(f"Force {a_name} and {b_name} to talk directly over layer 2"):
        # on-link routes + empty ARP caches: a reply could only come from the same VLAN
        force_layer2_path(a, b)

    with step(f"Start a packet capture on {b_name}"):
        capture = start_capture(lab, b, "p1", "icmp or arp")

    with step(f"Send {isolation_probe_count()} pings from {a_name} to {b_name}"):
        result = send_isolation_probes(a, b)

    with step(f"Check {b_name} never answered ({result.received}/{result.sent} replies)"):
        check_no_replies(a, b, result, capture)

    with step(f"Check {a_name}'s ARP requests for {b_name} went out unanswered"):
        # no neighbor entry would mean no probe left a, and silence would prove nothing
        check_probes_reached_the_wire(a, b)

    target = control_target(a)  # e.g. h2, also in VLAN 10 on sw1
    with step(
        f"Check {a_name} reaches {target.name} ({target.ip}), in its own VLAN, over layer 2"
        if target
        else f"No host or gateway shares {a_name}'s VLAN: its path cannot be proven"
    ):
        # without this, the silence above could mean a's own link or port is broken
        check_layer2_control(a, b, target)


def test_hosts_reach_each_other(lab: Lab, reachable_pair: tuple[str, str]) -> None:
    """Two hosts the intent connects can ping each other."""
    a_name, b_name = reachable_pair

    with step(f"Look up {b_name}'s address in the intent"):
        address = lab.intent.host_ip(b_name)

    timeout = lab.timing["reachability"].timeout
    with step(f"Ping {address} from {a_name} until {b_name} answers (up to {timeout:.0f}s)"):
        assert_reachable(lab[a_name], lab[b_name])
