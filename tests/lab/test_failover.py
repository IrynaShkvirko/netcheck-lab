"""Resilience: primary link failure, measured with traffic (ADR-0004)."""

import ipaddress

import pytest

from netcheck.convergence import measure_reconvergence
from netcheck.lab import Lab

pytestmark = pytest.mark.resilience


def test_primary_link_failure_reroutes_via_backup(lab: Lab) -> None:
    intent = lab.intent
    primary = intent.link_by_label("primary")
    backup = intent.link_by_label("backup")
    r1 = lab[primary.a.node]
    backup_hop = backup.peer(r1.name).ip
    assert backup_hop
    # the prefix behind the far router, and a host on it
    far_lan = next(lk for lk in intent.links if lk.ospf_passive)
    prefix = str(ipaddress.ip_interface(far_lan.a.ip or far_lan.b.ip or "").network)
    dst = lab[far_lan.b.node if intent.nodes[far_lan.b.node].role == "host" else far_lan.a.node]
    src = lab[intent.reachable[0][0]]
    port = primary.endpoint(r1.name).port

    def fail_primary() -> None:
        r1.links.set_link(port, up=False)
        lab.cleanup.register(
            f"bring {r1.name}:{port} back up", lambda: r1.links.set_link(port, up=True)
        )

    result = measure_reconvergence(
        check="reconverge_local_link",
        src=src,
        dst=dst,
        router=r1,
        prefix=prefix,
        expected_next_hop=backup_hop.split("/")[0],
        action=fail_primary,
    )

    print(
        f"data-plane outage {result.data_plane_s}s, control plane {result.control_plane_s}s, "
        f"{result.stream.loss} of {result.stream.sent} packets lost"
    )
