"""Every rule in netcheck.model.intent.validate, one broken intent each: the intent is the input
to device commands, so each rule must reject what it says and accept its boundary values."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from netcheck.model.intent import Intent, IntentError, load_intent

from .intents import ROOT

CAMPUS: dict[str, Any] = yaml.safe_load((ROOT / "intent" / "lab.yaml").read_text())
Edit = Callable[[dict[str, Any]], None]


def load(tmp_path: Path, edit: Edit) -> Intent:
    raw = copy.deepcopy(CAMPUS)
    edit(raw)
    path = tmp_path / "intent.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_intent(path)


def link(raw: dict[str, Any], name: str) -> dict[str, Any]:
    return next(lk for lk in raw["links"] if lk.get("label") == name)


def node(name: str, **fields: Any) -> Edit:
    return lambda raw: raw["nodes"][name].update(fields)


def without(name: str, key: str) -> Edit:
    return lambda raw: raw["nodes"][name].pop(key)


def on_link(name: str, **fields: Any) -> Edit:
    return lambda raw: link(raw, name).update(fields)


def on_side(name: str, side: str, **fields: Any) -> Edit:
    return lambda raw: link(raw, name)[side].update(fields)


def links(i: int, **fields: Any) -> Edit:
    return lambda raw: raw["links"][i].update(fields)


BROKEN: list[tuple[str, Edit, str]] = [
    (
        "hello 0",
        lambda r: r["ospf"].update(hello_interval=0),
        "ospf: need 0 < hello_interval < dead_interval, got 0/4",
    ),
    (
        "dead = hello",
        lambda r: r["ospf"].update(hello_interval=4),
        "ospf: need 0 < hello_interval < dead_interval, got 4/4",
    ),
    ("negative area", lambda r: r["ospf"].update(area=-1), "ospf: area must be >= 0, got -1"),
    (
        "unknown role",
        node("sw1", role="firewall"),
        "node sw1: role 'firewall' is not one of router, switch, host",
    ),
    (
        "bad router id",
        node("r1", router_id="one"),
        "router r1: router_id 'one' is not an IPv4 address",
    ),
    (
        "no router id",
        without("r1", "router_id"),
        "router r1: router_id None is not an IPv4 address",
    ),
    (
        "shared router id",
        node("r2", router_id="1.1.1.1"),
        "router r2: router_id 1.1.1.1 also used by r1",
    ),
    ("host without ip", without("h1", "ip"), "host h1: needs an ip"),
    (
        "host ip without prefix",
        node("h1", ip="10.0.10.11"),
        "host h1 ip: '10.0.10.11' must be IPv4 with a prefix length",
    ),
    (
        "host ip not an address",
        node("h1", ip="ten/24"),
        "host h1 ip: 'ten/24' is not an address with prefix length (a.b.c.d/len)",
    ),
    (
        "bad gateway",
        node("h1", gateway="gw"),
        "host h1: gateway 'gw' is not an IPv4 address",
    ),
    (
        "gateway off the subnet",
        node("h1", gateway="10.0.99.1"),
        "host h1: gateway 10.0.99.1 is outside 10.0.10.0/24",
    ),
    (
        "vlan 0",
        lambda r: r["vlans"].update({0: {"name": "zero"}}),
        "vlan 0: id must be 1-4094",
    ),
    (
        "vlan 4095",
        lambda r: r["vlans"].update({4095: {"name": "high"}}),
        "vlan 4095: id must be 1-4094",
    ),
    (
        "gateway on a switch",
        lambda r: r["vlans"][10]["gateway"].update(node="sw1"),
        "vlan 10: gateway node sw1 is not a router",
    ),
    (
        "gateway on no node",
        lambda r: r["vlans"][10]["gateway"].update(node="r9"),
        "vlan 10: gateway node r9 is not a router",
    ),
    (
        "gateway ip without prefix",
        lambda r: r["vlans"][10]["gateway"].update(ip="10.0.10.1"),
        "vlan 10 gateway ip: '10.0.10.1' must be IPv4 with a prefix length",
    ),
    (
        "unknown node on a link",
        lambda r: r["links"][0]["b"].update(node="sw9"),
        "link h1:p1-sw9:p1: unknown node sw9",
    ),
    (
        "bad port name",
        lambda r: r["links"][0]["b"].update(port="eth1"),
        "link h1:p1-sw1:eth1: port 'eth1' must look like p1, p2, ...",
    ),
    (
        "port used twice",
        lambda r: r["links"][1]["b"].update(port="p1"),
        "port sw1:p1 used twice",
    ),
    (
        "unknown mode",
        links(0, mode="hybrid"),
        "link h1:p1-sw1:p1: mode 'hybrid' must be access, trunk or absent (routed)",
    ),
    ("unknown access vlan", links(0, vlan=99), "access link h1-sw1 uses unknown VLAN 99"),
    ("unknown trunk vlan", links(3, vlans=[10, 30]), "trunk sw1-r1 carries unknown VLAN 30"),
    (
        "cost 0",
        on_link("primary", ospf_cost=0),
        "link r1:p2-r2:p1: ospf_cost must be a positive integer, got 0",
    ),
    (
        "fractional cost",
        on_link("primary", ospf_cost=1.5),
        "link r1:p2-r2:p1: ospf_cost must be a positive integer, got 1.5",
    ),
    (
        "router link without cost",
        lambda r: link(r, "backup").pop("ospf_cost"),
        "link r1:p3-r2:p2: a link between two routers needs ospf_cost "
        "(it decides the path, and the tests check the path)",
    ),
    (
        "link ip without prefix",
        on_side("primary", "a", ip="10.0.12.0"),
        "link r1:p2-r2:p1 r1 ip: '10.0.12.0' must be IPv4 with a prefix length",
    ),
    (
        "link ends on two subnets",
        on_side("primary", "b", ip="10.0.13.1/31"),
        "routed link r1-r2: 10.0.12.0/31 != 10.0.13.0/31",
    ),
    (
        "label used twice",
        on_link("backup", label="primary"),
        "link label 'primary' used more than once",
    ),
    (
        "same host twice",
        lambda r: r["expect"]["reachable"].append(["h1", "h1"]),
        "expectation pair [h1, h1] names the same host twice",
    ),
    (
        "expectation on a router",
        lambda r: r["expect"]["isolated"].append(["h1", "r1"]),
        "expectation references non-host r1",
    ),
    (
        "expectation on no node",
        lambda r: r["expect"]["reachable"].append(["h1", "h9"]),
        "expectation references non-host h9",
    ),
]


@pytest.mark.parametrize(("edit", "message"), [b[1:] for b in BROKEN], ids=[b[0] for b in BROKEN])
def test_each_rule_rejects_what_it_says(tmp_path: Path, edit: Edit, message: str) -> None:
    with pytest.raises(IntentError) as exc:
        load(tmp_path, edit)
    assert message in str(exc.value).split("; ")


def test_every_problem_is_reported_at_once(tmp_path: Path) -> None:
    def two(raw: dict[str, Any]) -> None:
        raw["ospf"]["area"] = -1
        raw["nodes"]["h1"].pop("ip")

    with pytest.raises(IntentError) as exc:
        load(tmp_path, two)
    assert str(exc.value) == "ospf: area must be >= 0, got -1; host h1: needs an ip"


def test_boundary_values_are_accepted(tmp_path: Path) -> None:
    def edges(raw: dict[str, Any]) -> None:
        raw["vlans"].update({1: {"name": "lowest"}, 4094: {"name": "highest"}})
        link(raw, "primary")["ospf_cost"] = 1
        raw["ospf"].update(area=0, hello_interval=1, dead_interval=2)

    intent = load(tmp_path, edges)
    assert {1, 4094} <= set(intent.vlans)
    assert intent.vlans[4094].vid == 4094
    assert intent.link_by_label("primary").ospf_cost == 1


def test_a_loaded_intent_keeps_its_source_and_expectations() -> None:
    intent = load_intent(ROOT / "intent" / "lab.yaml")
    assert intent.raw == CAMPUS
    assert intent.reachable == [("h1", "h2"), ("h1", "h4"), ("h2", "h4")]
    assert len(intent.digest) == 12
    assert intent.timer_vars() == {"hello_interval": 1.0, "dead_interval": 4.0}


def test_an_intent_without_reachability_expectations_loads(tmp_path: Path) -> None:
    assert load(tmp_path, lambda r: r["expect"].pop("reachable")).reachable == []


def test_lookups_name_what_is_missing() -> None:
    intent = load_intent(ROOT / "intent" / "lab.yaml")
    with pytest.raises(KeyError, match="no link at r1:p9"):
        intent.link_at("r1", "p9")
    with pytest.raises(KeyError, match="no link labelled 'spare'"):
        intent.link_by_label("spare")
    with pytest.raises(KeyError, match="'h4'"):
        intent.link_by_label("primary").endpoint("h4")
    with pytest.raises(IntentError, match="sw1 has no ip"):
        intent.host_ip("sw1")
