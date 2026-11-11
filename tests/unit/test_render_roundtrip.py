"""What the rendered lab would do, checked for every intent without a lab.

The golden files say the render did not change; these tests say it is right. Each one
reads render's output back the way the device would (FRR config through the local
backend's parser, switch commands through a small model of the Linux bridge, host and
router commands through what iproute2 does with them) and compares the result with the
intent or with Expected. The models are written from kernel and iproute2 behaviour, not
from model/render.py, so a render bug and a matching Expected bug cannot both hide here.
"""

from __future__ import annotations

import ipaddress
import shlex
from pathlib import Path
from typing import Any

import pytest

from netcheck.backends.local import frr_sections, parse_bridge_vlans, parse_frr_interfaces
from netcheck.model.expected import Expected
from netcheck.model.intent import Intent, load_intent
from netcheck.model.ports import PortMap
from netcheck.model.render import BRIDGE, frr_config, host_exec, router_exec, switch_exec

from .intents import INTENT_FILES, intent_id


@pytest.fixture(params=INTENT_FILES, ids=intent_id)
def intent(request: pytest.FixtureRequest) -> Intent:
    path: Path = request.param
    return load_intent(path)


def _nodes(intent: Intent, role: str) -> list[str]:
    return sorted(n.name for n in intent.by_role(role))


# ---- routers ------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["eth", "swp"])
def test_rendered_frr_config_parses_back_to_the_intended_interfaces(
    intent: Intent, prefix: str
) -> None:
    """The FRR writer (render) and the FRR reader (local backend) agree with Expected."""
    expected = Expected(intent)
    for router in _nodes(intent, "router"):
        ports = PortMap.for_node(intent, router, prefix)
        parsed = parse_frr_interfaces(frr_config(intent, router, prefix), ports)
        assert parsed == expected.ospf_interfaces(router), router


def test_rendered_frr_config_sets_the_router_id(intent: Intent) -> None:
    for router in _nodes(intent, "router"):
        sections = frr_sections(frr_config(intent, router))
        rid = intent.nodes[router].router_id
        assert f"ospf router-id {rid}" in sections.get("router ospf", []), router


def test_every_vlan_gateway_interface_is_created_with_its_vlan_id(intent: Intent) -> None:
    """FRR only addresses an interface that exists: each sub-interface in the config
    (eth1.10) must be created by the router's commands, on its parent, with that VLAN ID."""
    for router in _nodes(intent, "router"):
        ports = PortMap.for_node(intent, router, "eth")
        created = {}
        for cmd in router_exec(intent, router):
            words = shlex.split(cmd)
            if words[:3] == ["ip", "link", "add"]:
                opts = dict(zip(words[3::2], words[4::2], strict=False))
                created[opts["name"]] = (opts["link"], int(opts["id"]))
        wanted = {
            ports.real(name): (ports.real(name.partition(".")[0]), int(name.partition(".")[2]))
            for name in Expected(intent).ospf_interfaces(router)
            if "." in name
        }
        assert created == wanted, router


# ---- switches: a small model of the Linux bridge ------------------------------------


class LinuxBridgeModel:
    """What the kernel does with the switch's `ip link` / `bridge vlan` commands.

    A port enslaved to a VLAN-aware bridge starts in VLAN 1 as PVID, egress untagged (the
    bridge's default_pvid). `bridge vlan add ... pvid` moves the PVID to that VLAN.
    A command the model does not know fails the test, so a new kind of command in render
    must come with a model of it.
    """

    def __init__(self) -> None:
        self.vlan_filtering = False
        self.bridge_up = False
        self.ports: dict[str, dict[int, set[str]]] = {}

    def run(self, cmd: str) -> None:
        w = shlex.split(cmd)
        if w[:3] == ["ip", "link", "add"] and w[4:6] == ["type", "bridge"]:
            assert w[3] == BRIDGE, cmd
            self.vlan_filtering = w[6:8] == ["vlan_filtering", "1"]
        elif w[:3] == ["ip", "link", "set"] and w[4:6] == ["master", BRIDGE]:
            self.ports[w[3]] = {1: {"PVID", "Egress Untagged"}}
        elif w[:3] == ["ip", "link", "set"] and w[4:] == ["up"]:
            if w[3] == BRIDGE:
                self.bridge_up = True
        elif w[:4] == ["bridge", "vlan", "del", "dev"] and w[5] == "vid":
            self.ports[w[4]].pop(int(w[6]), None)
        elif w[:4] == ["bridge", "vlan", "add", "dev"] and w[5] == "vid":
            vlans, vid = self.ports[w[4]], int(w[6])
            flags = vlans.setdefault(vid, set())
            if "pvid" in w[7:]:
                for other in vlans.values():
                    other.discard("PVID")
                flags.add("PVID")
            if "untagged" in w[7:]:
                flags.add("Egress Untagged")
        else:
            raise AssertionError(f"the bridge model does not know {cmd!r}")

    def json(self) -> list[dict[str, Any]]:
        """The ports as `bridge -j vlan show` lists them."""
        return [
            {
                "ifname": port,
                "vlans": [{"vlan": vid, "flags": sorted(f)} for vid, f in sorted(vlans.items())],
            }
            for port, vlans in sorted(self.ports.items())
        ]


def test_switch_commands_give_each_port_exactly_its_intended_vlans(intent: Intent) -> None:
    """Every switch port carries exactly the intended VLANs, as the local backend would
    read them back from the bridge, and the bridge filters by VLAN at all."""
    expected = Expected(intent)
    for switch in _nodes(intent, "switch"):
        model = LinuxBridgeModel()
        for cmd in switch_exec(intent, switch):
            model.run(cmd)
        assert model.vlan_filtering, f"{switch}: bridge without vlan_filtering ignores VLANs"
        assert model.bridge_up, f"{switch}: bridge never set up"
        live = parse_bridge_vlans(model.json(), PortMap.for_node(intent, switch, "eth"))
        assert live == expected.vlan_membership(switch), switch


# ---- hosts ----------------------------------------------------------------------------


def test_host_commands_give_each_host_its_intended_address_and_routes(intent: Intent) -> None:
    for host in _nodes(intent, "host"):
        node = intent.nodes[host]
        (link,) = intent.links_of(host)
        ifname = PortMap.for_node(intent, host, "eth").real(link.endpoint(host).port)
        addresses, up, gateway, mgmt_default_removed = [], False, None, False
        for cmd in host_exec(intent, host):
            w = shlex.split(cmd)
            if w[:3] == ["ip", "addr", "add"]:
                assert w[4:] == ["dev", ifname], cmd
                addresses.append(ipaddress.ip_interface(w[3]))
            elif w[:3] == ["ip", "link", "set"] and w[3:] == [ifname, "up"]:
                up = True
            elif w[:4] == ["ip", "route", "replace", "default"]:
                assert w[4] == "via" and w[6:] == ["dev", ifname], cmd
                gateway = w[5]
            elif w == ["ip", "route", "del", "default", "dev", "eth0"]:
                mgmt_default_removed = True
            else:
                raise AssertionError(f"{host}: unexpected command {cmd!r}")
        assert addresses == [ipaddress.ip_interface(node.ip or "")], host
        assert up, f"{host}: {ifname} never set up"
        assert gateway == node.gateway, host
        # Docker gives every container a default route via the management network: a host
        # with a gateway replaces it, a host without one must lose it (no route off-subnet)
        assert mgmt_default_removed == (node.gateway is None), host
