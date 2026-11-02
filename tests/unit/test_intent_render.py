from pathlib import Path

import pytest
import yaml

from netcheck.intent import IntentError, expected_state, load_intent, validate
from netcheck.render import frr_config, port_name, render, switch_exec


def test_logical_ports_map_per_backend() -> None:
    assert port_name("p3", "eth") == "eth3"
    assert port_name("p3", "swp") == "swp3"
    with pytest.raises(ValueError, match="logical ports"):
        port_name("eth3", "eth")


def test_expected_state_from_intent(intent):  # type: ignore[no-untyped-def]
    exp = expected_state(intent)
    assert exp["routes"] == {"r1": ["10.0.30.0/24"], "r2": ["10.0.10.0/24"]}
    assert {n["neighbor"] for n in exp["ospf_neighbors"]["r1"]} == {"r2"}
    assert len(exp["ospf_neighbors"]["r1"]) == 2  # primary + backup
    assert [(n["local_port"], n["link"]) for n in exp["ospf_neighbors"]["r1"]] == [
        ("p2", "primary"),
        ("p3", "backup"),
    ]
    assert exp["vlan_membership"]["sw1"]["p3"] == {"mode": "access", "vlans": [20]}
    assert exp["vlan_membership"]["sw1"]["p4"] == {"mode": "trunk", "vlans": [10, 20]}


def test_frr_config_carries_intent_timers(intent):  # type: ignore[no-untyped-def]
    cfg = frr_config(intent, "r1")
    assert "interface eth2" in cfg and " ip ospf cost 10" in cfg
    assert " ip ospf network point-to-point" in cfg
    assert " ip ospf dead-interval 4" in cfg
    assert "interface eth1.10" in cfg and " ip ospf passive" in cfg


def test_switch_ports_get_intended_vlans(intent):  # type: ignore[no-untyped-def]
    cmds = switch_exec(intent, "sw1")
    assert "bridge vlan add dev eth3 vid 20 pvid untagged" in cmds
    assert "bridge vlan add dev eth4 vid 10" in cmds


def test_render_writes_three_outputs(intent, tmp_path: Path):  # type: ignore[no-untyped-def]
    images = {"router": "frr", "switch": "mt", "host": "mt"}
    topo = render(intent, images, tmp_path)
    clab = yaml.safe_load(topo.read_text())
    assert set(clab["topology"]["nodes"]) == set(intent.nodes)
    assert {"endpoints": ["r1:eth2", "r2:eth1"]} in clab["topology"]["links"]
    assert (tmp_path / "configs" / "r2" / "frr.conf").exists()
    assert (tmp_path / "expected_state.json").exists()


def test_intent_validation_catches_unknown_vlan(intent):  # type: ignore[no-untyped-def]
    bad = intent.links[0]
    object.__setattr__(bad, "vlan", 99)
    with pytest.raises(IntentError, match="unknown VLAN 99"):
        validate(intent)


def test_intent_digest_is_stable() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (
        load_intent(root / "intent/lab.yaml").digest == load_intent(root / "intent/lab.yaml").digest
    )
