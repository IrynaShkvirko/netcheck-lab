"""The lab across commands: reset, faults and render through the backend (ADR-0003)."""

from __future__ import annotations

from pathlib import Path

import pytest

from netcheck.backends.base import SnapshotMismatch
from netcheck.backends.local import LocalBackend
from netcheck.backends.nvidia_air import NvidiaAirBackend

IMAGES = {"router": "frr", "switch": "mt", "host": "mt"}


def test_restoring_the_baseline_clears_the_fault_ledger(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    backend = lab.backend
    _, baseline = backend.up()
    backend.inject_fault("wrong-vlan")
    assert backend.state.active_faults()
    assert backend.reset(baseline) == baseline  # by its ID, as `pytest --snapshot` does
    assert backend.state.active_faults() == []


def test_reset_without_an_id_restores_the_baseline(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    _, baseline = lab.backend.up()
    lab.backend.inject_fault("wrong-vlan")
    assert lab.backend.reset() == baseline
    assert lab.backend.state.active_faults() == []
    assert parts["snapshot_sw1"].restores == 1


def test_restoring_another_snapshot_keeps_the_ledger(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    lab.backend.up()
    lab.backend.inject_fault("wrong-vlan")
    with_fault = lab.backend.snapshot("manual")  # taken while the fault is applied
    lab.backend.reset(with_fault)
    assert len(lab.backend.state.active_faults()) == 1


def test_reset_before_any_baseline_is_refused(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    with pytest.raises(SnapshotMismatch, match="no baseline yet"):
        lab.backend.reset()


def test_inject_fault_changes_the_lab_and_records_it(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    description = lab.backend.inject_fault("wrong-vlan")
    assert description.startswith("sw1:p3 (h3) moved from VLAN 20 to VLAN 10")
    assert parts["bridge"].ports["p3"].vlans == (10,)
    (entry,) = lab.backend.state.active_faults()
    assert entry.startswith(f"wrong-vlan: {description}")


def test_local_render_uses_the_backend_port_prefix(intent, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    backend = LocalBackend(intent, {"images": IMAGES, "port_prefix": "swp"}, tmp_path)
    topo = backend.render()
    assert topo == tmp_path / "build" / intent.name / f"{intent.name}.clab.yml"
    assert "r1:swp2" in topo.read_text()
    assert (backend.build_dir / "expected_state.json").exists()


def test_nvidia_render_says_it_is_not_implemented(intent, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    backend = NvidiaAirBackend(intent, {}, tmp_path)
    with pytest.raises(NotImplementedError, match="Air topology generation"):
        backend.render()


def test_cli_render_on_nvidia_exits_1(monkeypatch, capsys, intent, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import netcheck.__main__ as cli

    monkeypatch.setattr(cli, "get_backend", lambda *_: NvidiaAirBackend(intent, {}, tmp_path))
    assert cli.main(["--backend", "nvidia", "render"]) == 1
    assert "Air topology generation" in capsys.readouterr().err


# ---- the real session fixture, on a fake backend --------------------------------------


def _session_run(pytester, monkeypatch, backend, *args: str):  # type: ignore[no-untyped-def]
    import netcheck.pytest_plugin as plugin

    repo = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(plugin, "get_backend", lambda *_: backend)
    pytester.makepyfile("def test_x(lab):\n    pass\n")
    return pytester.runpytest_inprocess(
        "-p", "netcheck.pytest_plugin", "--intent", str(repo / "intent/lab.yaml"), *args
    )


def test_pytest_snapshot_of_the_baseline_clears_faults(pytester, monkeypatch, fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    _, baseline = lab.backend.up()
    lab.backend.inject_fault("wrong-vlan")
    result = _session_run(pytester, monkeypatch, lab.backend, "--snapshot", baseline)
    result.assert_outcomes(passed=1)
    assert lab.backend.state.active_faults() == []


def test_pytest_snapshot_with_an_unknown_id_is_a_usage_error(pytester, monkeypatch, fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    result = _session_run(pytester, monkeypatch, lab.backend, "--snapshot", "nope")
    assert result.ret == 4
    result.stdout.fnmatch_lines(["*--snapshot: no snapshot nope*"])


# ---- reuse the running lab, or deploy it (ADR-0003) -------------------------------------


def _built_from_this_intent(backend) -> None:  # type: ignore[no-untyped-def]
    backend.state.set_deployed_intent(backend.intent.digest)


def test_intact_running_lab_is_reused(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    _built_from_this_intent(lab.backend)
    lab.backend.state.set_baseline("baseline-kept")
    assert lab.backend.up() == (False, "baseline-kept")
    assert lab.backend.deploys == 0


def test_lab_built_from_another_intent_is_redeployed(fake, capsys) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    lab.backend.state.set_deployed_intent("0123abcd")
    lab.backend.state.set_baseline("baseline-old")
    deployed, baseline = lab.backend.up()
    assert deployed and baseline != "baseline-old" and lab.backend.deploys == 1
    assert lab.backend.state.deployed_intent() == lab.intent.digest
    assert "built from intent 0123abcd" in capsys.readouterr().out


def test_degraded_lab_is_redeployed(fake, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    _built_from_this_intent(lab.backend)
    monkeypatch.setattr(lab["r1"].links, "link_states", lambda ports: {})  # links lost
    deployed, _ = lab.backend.up()
    assert deployed and lab.backend.deploys == 1
    assert "degraded (missing r1:p1 (eth1)" in capsys.readouterr().out


def test_lab_that_is_not_running_is_deployed_quietly(fake, capsys) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    lab.backend.running = False
    deployed, _ = lab.backend.up()
    assert deployed and lab.backend.deploys == 1
    assert lab.backend.state.deployed_intent() == lab.intent.digest
    assert capsys.readouterr().out == ""


def test_fresh_redeploys_an_intact_lab(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    _built_from_this_intent(lab.backend)
    deployed, _ = lab.backend.up(fresh=True)
    assert deployed and lab.backend.deploys == 1
