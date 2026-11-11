"""One lab test's run, without pytest: start, teardown outcome, failure evidence."""

from __future__ import annotations

import json
import time

from netcheck.run.report import FailureReport, IntentViolation
from netcheck.run.steps import step


def test_start_clears_old_artifacts_and_takes_a_snapshot(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    old = lab.artifacts_dir / "tests_x.py_test_a" / "report.txt"
    old.parent.mkdir(parents=True)
    old.write_text("from an earlier run")
    run = lab.start_test("tests/x.py::test_a", "test_a")
    assert run.artifacts == old.parent and not old.exists()
    assert run.snapshot_id.startswith("test_a-")
    assert (lab.backend.snapshot_dir / f"{run.snapshot_id}.json").exists()
    assert lab.cleanup is run.cleanup and lab.test_artifacts == run.artifacts


def test_end_undoes_test_actions_before_restoring_the_snapshot(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    run = lab.start_test("t", "t")
    order = []
    run.cleanup.register("first", lambda: order.append(("first", parts["snapshot_r1"].restores)))
    run.cleanup.register("second", lambda: order.append(("second", parts["snapshot_r1"].restores)))
    outcome = run.end()
    assert outcome.ok
    assert order == [("second", 0), ("first", 0)]  # LIFO, and before the restore
    assert parts["snapshot_r1"].restores == 1


def test_failed_undo_is_reported_but_the_lab_is_still_restored(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    run = lab.start_test("t", "t")
    run.cleanup.register("undo that breaks", lambda: 1 / 0)
    outcome = run.end()
    assert not outcome.ok and not outcome.lab_untrusted
    assert outcome.listing() == "undo that breaks: ZeroDivisionError: division by zero"
    assert parts["snapshot_r1"].restores == 1
    assert (run.artifacts / "cleanup-errors.txt").read_text() == outcome.listing() + "\n"


def test_failed_restore_makes_the_lab_untrusted(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    run = lab.start_test("t", "t")
    parts["snapshot_r1"].fail_with = RuntimeError("frr gone")
    outcome = run.end()
    assert outcome.lab_untrusted
    assert outcome.listing().startswith("snapshot restore: RestoreFailed: ")
    assert "r1: RuntimeError: frr gone" in outcome.listing()


def test_failed_check_report_is_completed_and_written(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    run = lab.start_test("tests/lab/test_vlan.py::test_x", "test_x")
    lab["r1"].log.record("exec", "vtysh -c 'show ip ospf neighbor json'", "{}", 0, time.time())
    with step("look up the ports"):
        pass
    report = run.record_failure(
        IntentViolation(FailureReport(verdict="h1 and h3 must be isolated")),
        "tests/lab/test_vlan.py::test_x",
    )
    assert report is not None
    assert report.steps[0].startswith("1. look up the ports [ok")
    assert report.snapshot_id == run.snapshot_id
    assert report.repro_command == (
        f"pytest 'tests/lab/test_vlan.py::test_x' --backend fake --snapshot {run.snapshot_id}"
    )
    assert report.environment["backend"] == "fake"
    assert any("show ip ospf neighbor json" in c for c in report.commands)
    assert (run.artifacts / "report.txt").read_text().startswith("INTENT VIOLATED: h1 and h3")
    assert json.loads((run.artifacts / "report.json").read_text())["snapshot_id"] == run.snapshot_id
    assert (run.artifacts / "logs" / "r1.jsonl").exists()


def test_other_failures_keep_device_logs_but_have_no_report(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    run = lab.start_test("t", "t")
    lab["r2"].log.record("exec", "ip -j link show", "[]", 0, time.time())
    assert run.record_failure(ConnectionError("r2 unreachable"), "t") is None
    assert (run.artifacts / "logs" / "r2.jsonl").exists()
    assert not (run.artifacts / "logs" / "r1.jsonl").exists()  # r1 made no calls in this test


def test_device_logs_from_before_the_test_are_left_out(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    lab["r1"].log.record("exec", "from an earlier test", "", 0, time.time())
    run = lab.start_test("t", "t")
    lab["r1"].log.record("exec", "from this test", "", 0, time.time())
    lines = run.device_log_lines()
    assert any("from this test" in ln for ln in lines)
    assert not any("from an earlier test" in ln for ln in lines)
