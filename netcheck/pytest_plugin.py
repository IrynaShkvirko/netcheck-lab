"""pytest integration: session lab, per-test isolation, failure reports (ADR-0003, ADR-0005)."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from netcheck.backends import get_backend, provenance
from netcheck.backends.base import SnapshotMismatch
from netcheck.backends.labstate import LabState
from netcheck.devices.device import CapabilityMissing
from netcheck.model.expected import Expected
from netcheck.model.intent import load_intent
from netcheck.run import measurements
from netcheck.run.lab import Lab, set_current
from netcheck.run.report import IntentViolation
from netcheck.run.testrun import TestRun
from netcheck.run.timing import Measurement, Timing

ROOT = Path(__file__).resolve().parent.parent

# the test's run, for the report hooks (set by _isolate on lab tests only)
RUN_KEY = pytest.StashKey[TestRun]()
MEASURED_KEY = pytest.StashKey[list[tuple[str, Measurement]]]()
# what the session's lab was (backend, intent, ...); set only by this plugin's own `lab`
SESSION_KEY = pytest.StashKey[dict[str, Any]]()

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
    """Show injected faults at the top of every run, so nobody misreads their results, and
    on the NVIDIA backend whether its parsers were ever checked against a real switch."""
    lines = []
    if config.getoption("--backend", default="local") == "nvidia":
        samples = ROOT / "tests" / "unit" / "fixtures" / "nvue"
        if samples.is_dir():
            status = provenance.status(samples)
            if not status.verified:
                lines.append(
                    f"netcheck: NVIDIA parsers checked against {status.line('NVUE')}; the rest "
                    "come from documentation, so this backend's results are unverified"
                )
    faults = LabState(ROOT).active_faults()
    if faults:
        lines.append(
            "netcheck: FAULTS ARE APPLIED to the lab (expected failures; `make lab-reset` clears):"
        )
        lines += [f"  {f}" for f in faults]
    return lines


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


@pytest.fixture(scope="session")
def lab(request: pytest.FixtureRequest) -> Generator[Lab, None, None]:
    cfg = request.config
    intent = load_intent(cfg.getoption("--intent"))
    backend = get_backend(cfg.getoption("--backend"), intent, ROOT)
    deployed, _ = backend.up(fresh=cfg.getoption("--fresh-lab"))
    if cfg.getoption("--snapshot"):
        try:
            backend.reset(cfg.getoption("--snapshot"))  # the baseline also clears faults
        except SnapshotMismatch as exc:
            # a wrong ID, not a broken lab: stop before any test, as a usage error
            pytest.exit(f"--snapshot: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)
    timing = Timing.load(ROOT / "timing.yaml", backend.timing_profile, intent.timer_vars())
    the_lab = Lab(
        intent=intent,
        expected=Expected(intent),
        backend=backend,
        devices=backend.devices(),
        timing=timing,
        artifacts_dir=Path(cfg.getoption("--artifacts")),
    )
    cfg.stash[SESSION_KEY] = {
        "backend": backend.name,
        "intent": intent.name,
        "intent_digest": intent.digest,
        "sla_mode": timing.sla_mode,
        "repeats": timing.repeats,
        "artifacts": the_lab.artifacts_dir,
    }
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
    run = the_lab.start_test(request.node.nodeid, request.node.name)
    request.node.stash[RUN_KEY] = run
    yield
    outcome = run.end()
    if outcome.ok:
        return
    if outcome.lab_untrusted:
        # the lab is in an unknown state: every later result would be blamed on the wrong test
        pytest.exit(
            f"lab restore failed after {request.node.nodeid}; stopping so later tests do not run "
            f"on a broken lab. Run `make lab-reset` (or `make lab-fresh`).\n{outcome.listing()}",
            returncode=EXIT_LAB_UNTRUSTED,
        )
    # rollback worked, but some undo step failed: show it as an error, not a passing test
    pytest.fail(
        f"cleanup failed after the test (lab was restored):\n{outcome.listing()}", pytrace=False
    )


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> Generator[None, object, object]:
    try:
        result = yield
    except CapabilityMissing as exc:
        pytest.skip(str(exc))
    # the test body ended normally: fail it now if any soft step failed along the way
    run = pyfuncitem.stash.get(RUN_KEY, None)
    combined = run.soft_failure_report() if run is not None else None
    if combined is not None:
        raise IntentViolation(combined)
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    rep = yield
    run = item.stash.get(RUN_KEY, None)
    if rep.when != "call" or run is None:
        return rep

    # measurements and context as JUnit <properties>, for passing tests too
    summary = item.config.stash.setdefault(MEASURED_KEY, [])
    for m in run.measured():
        summary.append((item.nodeid, m))
        item.user_properties.append((f"measured.{m.check}_s", m.value_s))
        if m.breached:
            item.user_properties.append((f"sla_breach.{m.check}", m.sla_s))
    item.user_properties.append(("backend", run.backend.name))
    item.user_properties.append(("intent", run.backend.intent.digest))
    item.user_properties.append(("snapshot_id", run.snapshot_id))

    if not rep.failed or call.excinfo is None:
        return rep

    report = run.record_failure(call.excinfo.value, item.nodeid)
    if report is not None:
        rep.longrepr = report.render()
    elif run.steps:
        # plain assertion or error: still say which step it was in
        rep.sections.append(("Captured stdout netcheck steps", "\n".join(run.step_lines()) + "\n"))

    log_text = "\n".join(run.device_log_lines())
    if log_text:
        # "Captured stdout" sections go to JUnit <system-out> (failed tests only, see pyproject)
        rep.sections.append(("Captured stdout netcheck device log", log_text + "\n"))
    return rep


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Keep this session's measurements: artifacts/measurements.json and one line in the
    history file (NETCHECK_HISTORY, default .netcheck/measurements.jsonl)."""
    context = session.config.stash.get(SESSION_KEY, None)
    measured = session.config.stash.get(MEASURED_KEY, [])
    if context is None or not measured:
        return
    context = dict(context)
    artifacts = Path(context.pop("artifacts"))
    history = ROOT / os.environ.get("NETCHECK_HISTORY", str(measurements.HISTORY_DEFAULT))
    measurements.save(measurements.run_record(measured, context), artifacts, history)


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
