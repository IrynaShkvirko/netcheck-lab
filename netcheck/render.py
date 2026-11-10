"""Generate the lab from the intent (ADR-0001): topology, device configs, expected state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from netcheck.expected import Expected
from netcheck.intent import Intent
from netcheck.ports import PortMap, port_name

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
    """FRR config for `router`: its intended OSPF interfaces (Expected), written out."""
    ports = PortMap.for_node(intent, router, prefix)
    lines = ["frr defaults traditional", f"hostname {router}", "!"]
    for name, want in Expected(intent).ospf_interfaces(router).items():
        port, _, vid = name.partition(".")
        if vid:
            description = f"vlan{vid}-{intent.vlans[int(vid)].name}-gateway"
        else:
            peer = intent.link_at(router, port).peer(router)
            description = f"to-{peer.node}-{peer.port}"
        lines += [f"interface {ports.real(name)}", f" description {description}"]
        lines += [f" ip address {a}" for a in want.addresses]
        lines.append(f" ip ospf area {want.area}")
        if want.passive:
            lines.append(" ip ospf passive")
        else:
            lines += [
                f" ip ospf network {want.network}",
                f" ip ospf hello-interval {want.hello_interval}",
                f" ip ospf dead-interval {want.dead_interval}",
            ]
            if want.cost is not None:
                lines.append(f" ip ospf cost {want.cost}")
        lines.append("!")
    router_id = intent.nodes[router].router_id
    lines += ["router ospf", f" ospf router-id {router_id}", "!", "line vty", "!"]
    return "\n".join(lines) + "\n"


def router_exec(intent: Intent, router: str, prefix: str = "eth") -> list[str]:
    """Create the VLAN sub-interfaces the router's OSPF interfaces need; FRR (zebra)
    assigns the addresses."""
    ports = PortMap.for_node(intent, router, prefix)
    cmds = []
    for name in Expected(intent).ospf_interfaces(router):
        port, _, vid = name.partition(".")
        if vid:
            parent = ports.real(port)
            cmds += [
                f"ip link add link {parent} name {parent}.{vid} type vlan id {vid}",
                f"ip link set {parent}.{vid} up",
            ]
    return cmds


# ---- Linux switch and hosts (executed by containerlab after start) ------

BRIDGE = "br0"  # the VLAN-aware bridge on Linux switches; not a port itself
MGMT_IFACE = "eth0"  # Containerlab's management interface on every node; never a lab port


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
        # replaces the default route Docker gives every container on the management network
        cmds.append(f"ip route replace default via {node.gateway} dev {ifname}")
    else:
        # no gateway in the intent means no route off the subnet: drop the management one
        # (Containerlab's eth0), or traffic to other subnets would leave through it
        cmds.append(f"ip route del default dev {MGMT_IFACE}")
    return cmds


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
    (out_dir / "expected_state.json").write_text(Expected(intent).to_json())
    return topo_path
