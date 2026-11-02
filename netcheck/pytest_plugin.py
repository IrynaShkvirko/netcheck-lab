"""pytest integration: session lab, per-test isolation, failure reports (ADR-0003, ADR-0005)."""

from __future__ import annotations

import re
import warnings
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from netcheck.backends import get_backend
from netcheck.cleanup import CleanupError
from netcheck.device import CapabilityMissing
from netcheck.intent import expected_state, load_intent
from netcheck.lab import Lab, set_current
from netcheck.report import IntentViolation
from netcheck.timing import Timing

ROOT = Path(__file__).resolve().parent.parent

LAB_KEY = pytest.StashKey[Lab]()
STATE_KEY = pytest.StashKey[dict[str, Any]]()


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


def _safe(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid).strip("_")[-120:]


@pytest.fixture(scope="session")
def lab(request: pytest.FixtureRequest) -> Generator[Lab, None, None]:
    cfg = request.config
    intent = load_intent(cfg.getoption("--intent"))
    backend = get_backend(cfg.getoption("--backend"), intent, ROOT)
    deployed = backend.deploy(fresh=cfg.getoption("--fresh-lab"))
    if cfg.getoption("--snapshot"):
        backend.restore(cfg.getoption("--snapshot"))
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
    the_lab.test_artifacts = the_lab.artifacts_dir / _safe(request.node.nodeid)
    the_lab.log_marks = {n: d.log.mark() for n, d in the_lab.devices.items()}
    request.node.stash[STATE_KEY] = {"measure_mark": len(the_lab.timing.measurements)}
    the_lab.snapshot_id = the_lab.backend.snapshot(_safe(request.node.name)[:40])
    request.node.stash[STATE_KEY]["snapshot_id"] = the_lab.snapshot_id
    yield
    errors = the_lab.cleanup.run()  # layer 2: LIFO undo
    try:
        the_lab.backend.restore(the_lab.snapshot_id)  # layer 1: config/link rollback
    except Exception as exc:  # noqa: BLE001
        errors.append(CleanupError("snapshot restore", f"{type(exc).__name__}: {exc}"))
    if errors:
        the_lab.test_artifacts.mkdir(parents=True, exist_ok=True)
        (the_lab.test_artifacts / "cleanup-errors.txt").write_text(
            "\n".join(f"{e.description}: {e.error}" for e in errors)
        )
        warnings.warn(f"cleanup errors in {request.node.nodeid}: {len(errors)}", stacklevel=1)


@pytest.hookimpl(wrapper=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> Generator[None, object, object]:
    try:
        return (yield)
    except CapabilityMissing as exc:
        pytest.skip(str(exc))


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
    for m in measured:
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
        if len(dev.log.entries) > marks.get(name, 0):
            dev.log.write_jsonl(artifacts / "logs" / f"{name}.jsonl", marks.get(name, 0))

    if isinstance(call.excinfo.value, IntentViolation):
        report = call.excinfo.value.report
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

    log_text = "\n".join(
        ln
        for name, dev in the_lab.devices.items()
        for ln in dev.log.summary(marks.get(name, 0), limit=200)
    )
    if log_text:
        # "Captured stdout" sections go to JUnit <system-out> (failed tests only, see pyproject)
        rep.sections.append(("Captured stdout netcheck device log", log_text + "\n"))
    return rep
