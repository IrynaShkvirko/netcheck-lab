"""pytest integration: session lab, per-test isolation, failure reports (ADR-0003, ADR-0005)."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from netcheck.backends import get_backend
from netcheck.backends.base import SnapshotMismatch
from netcheck.cleanup import CleanupError
from netcheck.device import CapabilityMissing
from netcheck.intent import expected_state, load_intent
from netcheck.lab import Lab, set_current
from netcheck.labstate import LabState
from netcheck.report import IntentViolation
from netcheck.steps import soft_failure_report
from netcheck.timing import Measurement, Timing

ROOT = Path(__file__).resolve().parent.parent

LAB_KEY = pytest.StashKey[Lab]()
STATE_KEY = pytest.StashKey[dict[str, Any]]()
MEASURED_KEY = pytest.StashKey[list[tuple[str, Measurement]]]()

# Exit code when the lab can no longer be trusted (restore failed); later results would mislead.
EXIT_LAB_UNTRUSTED = 3


def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("netcheck")
    g.addoption("--backend", default="local", choices=["local", "nvidia"])
    g.addoption("--intent", default=str(ROOT / "intent" / "lab.yaml"))
    g.addoption("--fresh-lab", action="store_true", help="redeploy even if the lab is running")
    g.addoption(
        "--destroy-lab",
        action="store_true",
        help="destroy the lab at session end if this session deployed it",
    )
    g.addoption(
        "--snapshot",
        default="",
        help="restore this snapshot ID before the session (reproduce a failure)",
    )
    g.addoption("--artifacts", default=str(ROOT / "artifacts"))


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Show injected faults at the top of every run, so nobody misreads their results."""
    faults = LabState(ROOT).active_faults()
    if not faults:
        return []
    return [
        "netcheck: FAULTS ARE APPLIED to the lab (expected failures; `make lab-reset` clears):"
    ] + [f"  {f}" for f in faults]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Refuse parallel workers for lab tests: they would drive the same containers,
    so faults, snapshots and restores of one worker would hit the others."""
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        return
    if any("lab" in getattr(item, "fixturenames", ()) for item in items):
        pytest.exit(
            "netcheck lab tests share one lab and must run serially: drop `-n` (pytest-xdist)",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )


def _safe(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid).strip("_")[-120:]


@pytest.fixture(scope="session")
def lab(request: pytest.FixtureRequest) -> Generator[Lab, None, None]:
    cfg = request.config
    intent = load_intent(cfg.getoption("--intent"))
    backend = get_backend(cfg.getoption("--backend"), intent, ROOT)
    deployed, _ = backend.up(fresh=cfg.getoption("--fresh-lab"))
    if cfg.getoption("--snapshot"):
        try:
            backend.restore(cfg.getoption("--snapshot"))
        except SnapshotMismatch as exc:
            # a wrong ID, not a broken lab: stop before any test, as a usage error
            pytest.exit(f"--snapshot: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)
    timing = Timing.load(ROOT / "timing.yaml", backend.timing_profile, intent.timer_vars())
    the_lab = Lab(
        intent=intent,
        expected=expected_state(intent),
        backend=backend,
        devices=backend.devices(),
        timing=timing,
        artifacts_dir=Path(cfg.getoption("--artifacts")),
    )
    cfg.stash[LAB_KEY] = the_lab
    token = set_current(the_lab)
    yield the_lab
    token.var.reset(token)
    if deployed and cfg.getoption("--destroy-lab"):
        backend.destroy()


@pytest.fixture(autouse=True)
def _isolate(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Layer 1 snapshot/rollback + layer 2 cleanup registry around every lab test."""
    if "lab" not in request.fixturenames:
        yield
        return
    the_lab: Lab = request.getfixturevalue("lab")
    # the report hook finds the lab here, whichever fixture created it
    request.config.stash[LAB_KEY] = the_lab
    the_lab.test_artifacts = the_lab.artifacts_dir / _safe(request.node.nodeid)
    # files from an earlier run of this test must not sit next to this run's evidence
    shutil.rmtree(the_lab.test_artifacts, ignore_errors=True)
    request.node.stash[STATE_KEY] = {"measure_mark": len(the_lab.timing.measurements)}
    the_lab.snapshot_id = the_lab.backend.snapshot(_safe(request.node.name)[:40])
    # mark logs after the snapshot, so reports show only the test's own calls
    the_lab.log_marks = {n: d.log.mark() for n, d in the_lab.devices.items()}
    the_lab.steps = []
    request.node.stash[STATE_KEY]["snapshot_id"] = the_lab.snapshot_id
    yield
    errors = the_lab.cleanup.run()  # layer 2: LIFO undo
    restore_failed = False
    try:
        the_lab.backend.restore(the_lab.snapshot_id)  # layer 1: config/link rollback
    except Exception as exc:  # noqa: BLE001
        restore_failed = True
        errors.append(CleanupError("snapshot restore", f"{type(exc).__name__}: {exc}"))
    if not errors:
        return
    the_lab.test_artifacts.mkdir(parents=True, exist_ok=True)
    listing = "\n".join(f"{e.description}: {e.error}" for e in errors)
    (the_lab.test_artifacts / "cleanup-errors.txt").write_text(listing + "\n")
    if restore_failed:
        # the lab is in an unknown state: every later result would be blamed on the wrong test
        pytest.exit(
            f"lab restore failed after {request.node.nodeid}; stopping so later tests do not run "
            f"on a broken lab. Run `make lab-reset` (or `make lab-fresh`).\n{listing}",
            returncode=EXIT_LAB_UNTRUSTED,
        )
    # rollback worked, but some undo step failed: show it as an error, not a passing test
    pytest.fail(f"cleanup failed after the test (lab was restored):\n{listing}", pytrace=False)


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> Generator[None, object, object]:
    try:
        result = yield
    except CapabilityMissing as exc:
        pytest.skip(str(exc))
    # the test body ended normally: fail it now if any soft step failed along the way
    the_lab = pyfuncitem.config.stash.get(LAB_KEY, None)
    if the_lab is not None and STATE_KEY in pyfuncitem.stash:
        combined = soft_failure_report(the_lab.steps)
        if combined is not None:
            raise IntentViolation(combined)
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    rep = yield
    the_lab = item.config.stash.get(LAB_KEY, None)
    state = item.stash.get(STATE_KEY, None)
    if rep.when != "call" or the_lab is None or state is None:
        return rep

    # measurements and context as JUnit <properties>, for passing tests too
    measured = the_lab.timing.measurements[state["measure_mark"] :]
    summary = item.config.stash.setdefault(MEASURED_KEY, [])
    for m in measured:
        summary.append((item.nodeid, m))
        item.user_properties.append((f"measured.{m.check}_s", m.value_s))
        if m.breached:
            item.user_properties.append((f"sla_breach.{m.check}", m.sla_s))
    item.user_properties.append(("backend", the_lab.backend.name))
    item.user_properties.append(("intent", the_lab.intent.digest))
    item.user_properties.append(("snapshot_id", state.get("snapshot_id", "")))

    if not rep.failed or call.excinfo is None:
        return rep

    artifacts = the_lab.test_artifacts
    marks = the_lab.log_marks
    for name, dev in the_lab.devices.items():
        if dev.log.mark() > marks.get(name, 0):
            dev.log.write_jsonl(artifacts / "logs" / f"{name}.jsonl", marks.get(name, 0))

    steps = [s.line() for s in the_lab.steps]
    if isinstance(call.excinfo.value, IntentViolation):
        report = call.excinfo.value.report
        report.steps = steps
        try:
            report.environment = the_lab.backend.environment()
        except Exception as exc:  # noqa: BLE001
            report.environment = {"backend": the_lab.backend.name, "env_error": str(exc)}
        report.measurements.update({m.check: m.value_s for m in measured})
        report.snapshot_id = state.get("snapshot_id", "")
        report.repro_command = (
            f"pytest '{item.nodeid}' --backend {the_lab.backend.name} "
            f"--snapshot {report.snapshot_id}"
        )
        report.artifacts_dir = str(artifacts)
        report.commands = [
            ln
            for name, dev in the_lab.devices.items()
            for ln in dev.log.summary(marks.get(name, 0), limit=4)
        ][-12:]
        report.write(artifacts)
        rep.longrepr = report.render()

    elif steps:
        # plain assertion or error: still say which step it was in
        rep.sections.append(("Captured stdout netcheck steps", "\n".join(steps) + "\n"))

    log_text = "\n".join(
        ln
        for name, dev in the_lab.devices.items()
        for ln in dev.log.summary(marks.get(name, 0), limit=200)
    )
    if log_text:
        # "Captured stdout" sections go to JUnit <system-out> (failed tests only, see pyproject)
        rep.sections.append(("Captured stdout netcheck device log", log_text + "\n"))
    return rep


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Print measured timing values so a passing run still shows the numbers."""
    measured = terminalreporter.config.stash.get(MEASURED_KEY, [])
    if not measured:
        return
    terminalreporter.section("netcheck measurements")
    for nodeid, m in measured:
        sla_s = f" (SLA {m.sla_s:g}s)" if m.sla_s is not None else ""
        flag = "  SLA BREACH" if m.breached else ""
        terminalreporter.write_line(
            f"{nodeid.split('::')[-1]}: {m.check} = {m.value_s:.3f}s{sla_s}{flag}"
        )
