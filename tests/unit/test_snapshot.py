"""Snapshot and restore through the Backend interface (ADR-0003), on fake devices."""

from __future__ import annotations

import json

import pytest

from netcheck.backends.base import LabDegraded, RestoreFailed, SnapshotMismatch
from netcheck.capabilities import PortVlans, Snapshot


def test_restore_brings_every_device_back(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    snap = lab.backend.snapshot("t")
    lab["r1"].links.set_link("p2", False)
    lab["sw1"].bridge.set_access_vlan("p3", 99)
    lab["r2"].ospf.set_interface_area("p3", None)
    lab.backend.restore(snap)
    assert lab["r1"].links.link_state("p2").admin_up
    assert lab["sw1"].bridge.vlan_membership()["p3"] != PortVlans("access", (99,))
    assert parts["ospf_r2"].area_overrides == {}


def test_snapshot_file_records_format_intent_and_backend(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    snap = lab.backend.snapshot("t")
    data = json.loads((lab.backend.snapshot_dir / f"{snap}.json").read_text())
    assert data["format"] == 2
    assert data["intent"] == lab.intent.digest
    assert data["backend"] == "fake"
    assert set(data["devices"]) == set(lab.devices)
    assert data["not_covered"] == []


def test_device_without_snapshot_capability_is_recorded_as_not_covered(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    del lab["h1"].capabilities[Snapshot]
    snap = lab.backend.snapshot("t")
    data = json.loads((lab.backend.snapshot_dir / f"{snap}.json").read_text())
    assert data["not_covered"] == ["h1"]
    assert "h1" not in data["devices"]
    lab.backend.restore(snap)  # the covered devices still restore


def _rewrite(lab, snap: str, **changes: object) -> None:  # type: ignore[no-untyped-def]
    path = lab.backend.snapshot_dir / f"{snap}.json"
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"intent": "0123abcd"}, "taken from intent 0123abcd"),
        ({"backend": "local"}, "taken on backend 'local'"),
        ({"format": None}, "older file format"),
    ],
)
def test_snapshot_from_another_lab_is_refused_before_anything_changes(  # type: ignore[no-untyped-def]
    fake, changes, message
) -> None:
    lab, parts = fake
    snap = lab.backend.snapshot("t")
    _rewrite(lab, snap, **changes)
    with pytest.raises(SnapshotMismatch, match=message):
        lab.backend.restore(snap)
    assert all(parts[f"snapshot_{n}"].restores == 0 for n in lab.devices)


def test_unknown_snapshot_id_is_a_mismatch(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    with pytest.raises(SnapshotMismatch, match="no snapshot nope"):
        lab.backend.restore("nope")


def test_degraded_lab_is_not_restored(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    snap = lab.backend.snapshot("t")
    monkeypatch.setattr(lab["r1"].links, "link_states", lambda ports: {})
    with pytest.raises(LabDegraded, match=r"r1:p2 \(eth2\)"):
        lab.backend.restore(snap)
    assert parts["snapshot_r2"].restores == 0


def test_every_device_is_tried_and_all_failures_are_listed(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    snap = lab.backend.snapshot("t")
    parts["snapshot_r1"].fail_with = RuntimeError("frr gone")
    parts["snapshot_sw1"].fail_with = RuntimeError("bridge gone")
    with pytest.raises(RestoreFailed) as exc:
        lab.backend.restore(snap)
    assert exc.value.errors == {
        "r1": "RuntimeError: frr gone",
        "sw1": "RuntimeError: bridge gone",
    }
    assert parts["snapshot_r2"].restores == 1  # the others were still restored


def test_cli_restore_of_a_snapshot_from_another_intent_exits_1(  # type: ignore[no-untyped-def]
    fake, monkeypatch, capsys
) -> None:
    import netcheck.__main__ as cli

    lab, _ = fake
    snap = lab.backend.snapshot("t")
    _rewrite(lab, snap, intent="0123abcd")
    monkeypatch.setattr(cli, "get_backend", lambda *_: lab.backend)
    assert cli.main(["restore", snap]) == 1
    assert "taken from intent 0123abcd" in capsys.readouterr().err
