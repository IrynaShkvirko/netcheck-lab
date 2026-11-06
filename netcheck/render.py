"""Generate the lab from the intent (ADR-0001): topology, device configs, expected state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from netcheck.intent import Intent, expected_state


def port_name(logical: str, prefix: str) -> str:
    """Map a logical port (p1) to a backend port (eth1, swp1)."""
    if not logical.startswith("p") or not logical[1:].isdigit():
        raise ValueError(f"logical ports look like p1, p2...; got {logical!r}")
    return f"{prefix}{logical[1:]}"


# ---- FRR ---------------------------------------------------------------

FRR_DAEMONS = """\
zebra=yes
ospfd=yes
bgpd=no
ospf6d=no
ripd=no
isisd=no
bfdd=no
vtysh_enable=yes
zebra_options="  -A 127.0.0.1 -s 90000000"
ospfd_options="  -A 127.0.0.1"
"""

VTYSH_CONF = "service integrated-vtysh-config\n"


def frr_config(intent: Intent, router: str, prefix: str = "eth") -> str:
    node = intent.nodes[router]
    o = intent.ospf
    lines = ["frr defaults traditional", f"hostname {router}", "!"]
    for lk in intent.links_of(router):
        ep = lk.endpoint(router)
        ifname = port_name(ep.port, prefix)
        if lk.mode == "trunk":
            for vid in lk.vlans:
                vlan = intent.vlans[vid]
                if vlan.gateway_node == router and vlan.gateway_ip:
                    lines += [
                        f"interface {ifname}.{vid}",
                        f" description vlan{vid}-{vlan.name}-gateway",
                        f" ip address {vlan.gateway_ip}",
                        f" ip ospf area {o.area}",
                        " ip ospf passive",
                        "!",
                    ]
            continue
        if not ep.ip:
            continue
        peer = lk.peer(router)
        lines += [
            f"interface {ifname}",
            f" description to-{peer.node}-{peer.port}",
            f" ip address {ep.ip}",
            f" ip ospf area {o.area}",
        ]
        if lk.ospf_passive:
            lines.append(" ip ospf passive")
        else:
            lines += [
                f" ip ospf network {o.network}",
                f" ip ospf hello-interval {o.hello_interval}",
                f" ip ospf dead-interval {o.dead_interval}",
            ]
            if lk.ospf_cost is not None:
                lines.append(f" ip ospf cost {lk.ospf_cost}")
        lines.append("!")
    lines += ["router ospf", f" ospf router-id {node.router_id}", "!", "line vty", "!"]
    return "\n".join(lines) + "\n"


# ---- Linux switch and hosts (executed by containerlab after start) ------

BRIDGE = "br0"  # the VLAN-aware bridge on Linux switches; not a port itself


def switch_exec(intent: Intent, switch: str, prefix: str = "eth") -> list[str]:
    cmds = [f"ip link add {BRIDGE} type bridge vlan_filtering 1", f"ip link set {BRIDGE} up"]
    for lk in intent.links_of(switch):
        ifname = port_name(lk.endpoint(switch).port, prefix)
        cmds += [
            f"ip link set {ifname} master {BRIDGE}",
            f"ip link set {ifname} up",
            f"bridge vlan del dev {ifname} vid 1",
        ]
        if lk.mode == "access":
            cmds.append(f"bridge vlan add dev {ifname} vid {lk.vlan} pvid untagged")
        elif lk.mode == "trunk":
            cmds += [f"bridge vlan add dev {ifname} vid {vid}" for vid in lk.vlans]
    return cmds


def host_exec(intent: Intent, host: str, prefix: str = "eth") -> list[str]:
    node = intent.nodes[host]
    (lk,) = intent.links_of(host)
    ifname = port_name(lk.endpoint(host).port, prefix)
    cmds = [f"ip addr add {node.ip} dev {ifname}", f"ip link set {ifname} up"]
    if node.gateway:
        cmds.append(f"ip route replace default via {node.gateway} dev {ifname}")
    return cmds


def router_exec(intent: Intent, router: str, prefix: str = "eth") -> list[str]:
    """Create VLAN sub-interfaces; FRR (zebra) assigns the addresses."""
    cmds = []
    for lk in intent.links_of(router):
        if lk.mode == "trunk":
            parent = port_name(lk.endpoint(router).port, prefix)
            for vid in lk.vlans:
                if intent.vlans[vid].gateway_node == router:
                    cmds += [
                        f"ip link add link {parent} name {parent}.{vid} type vlan id {vid}",
                        f"ip link set {parent}.{vid} up",
                    ]
    return cmds


# ---- Containerlab topology ---------------------------------------------


def clab_topology(intent: Intent, images: dict[str, str], prefix: str = "eth") -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    for name, node in intent.nodes.items():
        spec: dict[str, Any] = {
            "kind": "linux",
            "image": images[node.role],
            "labels": {"netcheck.role": node.role},
        }
        if node.role == "router":
            spec["binds"] = [
                f"configs/{name}/daemons:/etc/frr/daemons",
                f"configs/{name}/frr.conf:/etc/frr/frr.conf",
                f"configs/{name}/vtysh.conf:/etc/frr/vtysh.conf",
            ]
            exec_cmds = router_exec(intent, name, prefix)
        elif node.role == "switch":
            exec_cmds = switch_exec(intent, name, prefix)
        else:
            exec_cmds = host_exec(intent, name, prefix)
        if exec_cmds:
            spec["exec"] = exec_cmds
        nodes[name] = spec
    links = [
        {
            "endpoints": [
                f"{lk.a.node}:{port_name(lk.a.port, prefix)}",
                f"{lk.b.node}:{port_name(lk.b.port, prefix)}",
            ]
        }
        for lk in intent.links
    ]
    return {"name": intent.name, "topology": {"nodes": nodes, "links": links}}


def render(intent: Intent, images: dict[str, str], out_dir: Path, prefix: str = "eth") -> Path:
    """Write topology, configs and expected state. Returns the topology path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for r in intent.by_role("router"):
        cfg = out_dir / "configs" / r.name
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "frr.conf").write_text(frr_config(intent, r.name, prefix))
        (cfg / "daemons").write_text(FRR_DAEMONS)
        (cfg / "vtysh.conf").write_text(VTYSH_CONF)
    topo_path = out_dir / f"{intent.name}.clab.yml"
    source = intent.source.name if intent.source else "intent"
    header = f"# Generated from {source} (intent {intent.digest}). Do not edit.\n"
    topo_path.write_text(
        header + yaml.safe_dump(clab_topology(intent, images, prefix), sort_keys=False)
    )
    (out_dir / "expected_state.json").write_text(json.dumps(expected_state(intent), indent=2))
    return topo_path
