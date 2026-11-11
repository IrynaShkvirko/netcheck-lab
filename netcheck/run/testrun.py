"""One lab test from the snapshot before it to the restore after it.

A TestRun owns everything that belongs to a single test: its steps, cleanup actions,
artifacts folder, snapshot ID and the marks that cut its own device log and
measurements out of the session's. It knows nothing about pytest; the plugin starts a
run per test, turns the teardown outcome into pytest calls, and asks the run to
complete a failed test's report (ADR-0003, ADR-0005, ADR-0006).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from netcheck.devices.device import Device
from netcheck.run.cleanup import CleanupError, CleanupRegistry
from netcheck.run.report import FailureReport, IntentViolation
from netcheck.run.timing import Measurement, Timing

if TYPE_CHECKING:
    from netcheck.backends.base import Backend


# ---- steps (ADR-0006) -------------------------------------------------------------


@dataclass
class StepRecord:
    number: int
    title: str
    status: str = "running"  # running | passed | failed
    seconds: float = 0.0
    # soft step: a failed check is recorded and the test goes on (see netcheck.run.steps)
    soft: bool = False
    failure: AssertionError | None = field(default=None, repr=False, compare=False)

    @property
    def continued(self) -> bool:
        """Failed, but the test kept going; the failure is reported when the test ends."""
        return self.failure is not None

    def line(self) -> str:
        mark = {"passed": "ok", "failed": "FAILED", "running": "..."}[self.status]
        if self.continued:
            mark += ", continued"
        text = f"{self.number}. {self.title} [{mark}, {self.seconds:.1f}s]"
        if self.failure is not None:
            exc = self.failure
            # the title already says what was checked; add what was found instead
            found = exc.report.actual if isinstance(exc, IntentViolation) else ""
            text += f" - actual: {found}" if found else f" - {failure_summary(exc)}"
        return text


def failure_summary(exc: AssertionError) -> str:
    """One line for an assertion failure: the violated intent, or the assert message."""
    if isinstance(exc, IntentViolation):
        return exc.report.verdict
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def soft_failure_report(steps: Sequence[StepRecord]) -> FailureReport | None:
    """One report for all soft steps that failed, or None if none did.

    Diffs, drift and captures from each failure are kept. Evidence is merged by title;
    a title already present with the same lines (the same switch read twice) is not
    repeated, and one with different lines gets the step number added. A plain `assert`
    failure becomes an evidence entry named after its step.
    """
    failed = [s for s in steps if s.failure is not None]
    if not failed:
        return None
    checked = sum(1 for s in steps if s.soft)
    verdicts = [failure_summary(s.failure) for s in failed if s.failure is not None]
    verdict = (
        verdicts[0]
        if len(verdicts) == 1
        else f"{len(verdicts)} checks failed: " + "; ".join(verdicts)
    )
    numbers = ", ".join(str(s.number) for s in failed)
    combined = FailureReport(
        verdict=verdict,
        expected=f"all {checked} checks pass",
        actual=f"{len(failed)} of {checked} failed (steps {numbers})",
    )
    only = failed[0].failure if len(failed) == 1 else None
    if isinstance(only, IntentViolation) and (only.report.expected or only.report.actual):
        # one failed check: its own expected/actual say more than "1 of N failed"
        combined.expected, combined.actual = only.report.expected, only.report.actual
    for s in failed:
        exc = s.failure
        if isinstance(exc, IntentViolation):
            r = exc.report
            combined.state_diff += r.state_diff
            combined.drift += r.drift
            combined.capture_summary += r.capture_summary
            combined.capture_files += r.capture_files
            for title, lines in r.evidence.items():
                if title not in combined.evidence:
                    combined.evidence[title] = list(lines)
                elif combined.evidence[title] != lines:
                    combined.evidence[f"{title} (step {s.number})"] = list(lines)
        elif exc is not None:
            combined.add_evidence(f"step {s.number}: {s.title}", [str(exc).strip()])
    return combined


# ---- the run ----------------------------------------------------------------------


def safe_name(test_id: str) -> str:
    """A test ID as a file name."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", test_id).strip("_")[-120:]


@dataclass
class TeardownOutcome:
    """What happened after the test: cleanup errors, and whether the restore failed."""

    errors: list[CleanupError]
    restore_failed: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def lab_untrusted(self) -> bool:
        """The restore failed: the lab is in an unknown state and later tests would mislead."""
        return self.restore_failed

    def listing(self) -> str:
        return "\n".join(f"{e.description}: {e.error}" for e in self.errors)


class TestRun:
    __test__ = False  # not a pytest test class

    def __init__(
        self,
        backend: Backend,
        devices: dict[str, Device],
        timing: Timing,
        artifacts: Path,
        snapshot_id: str = "",
    ) -> None:
        self.backend = backend
        self.devices = devices
        self.timing = timing
        self.artifacts = artifacts
        self.snapshot_id = snapshot_id
        self.cleanup = CleanupRegistry()
        self.steps: list[StepRecord] = []
        self.log_marks = {n: d.log.mark() for n, d in devices.items()}
        self.measure_mark = len(timing.measurements)

    @classmethod
    def begin(
        cls,
        backend: Backend,
        devices: dict[str, Device],
        timing: Timing,
        artifacts_dir: Path,
        test_id: str,
        label: str,
    ) -> TestRun:
        """Clear the test's old artifacts and take the snapshot it is rolled back to."""
        artifacts = artifacts_dir / safe_name(test_id)
        # files from an earlier run of this test must not sit next to this run's evidence
        shutil.rmtree(artifacts, ignore_errors=True)
        snapshot_id = backend.snapshot(safe_name(label)[:40])
        # logs are marked after the snapshot, so reports show only the test's own calls
        return cls(backend, devices, timing, artifacts, snapshot_id)

    def end(self) -> TeardownOutcome:
        """Undo the test's actions (LIFO), then restore the snapshot. Never raises."""
        errors = self.cleanup.run()
        restore_failed = False
        try:
            self.backend.restore(self.snapshot_id)
        except Exception as exc:  # noqa: BLE001
            restore_failed = True
            errors.append(CleanupError("snapshot restore", f"{type(exc).__name__}: {exc}"))
        outcome = TeardownOutcome(errors, restore_failed)
        if not outcome.ok:
            self.artifacts.mkdir(parents=True, exist_ok=True)
            (self.artifacts / "cleanup-errors.txt").write_text(outcome.listing() + "\n")
        return outcome

    def soft_failure_report(self) -> FailureReport | None:
        """All soft steps that failed so far, as one report (None if none did)."""
        return soft_failure_report(self.steps)

    def measured(self) -> list[Measurement]:
        """Measurements taken during this test."""
        return self.timing.measurements[self.measure_mark :]

    def step_lines(self) -> list[str]:
        return [s.line() for s in self.steps]

    def device_log_lines(self, limit: int = 200) -> list[str]:
        return [
            ln
            for name, dev in self.devices.items()
            for ln in dev.log.summary(self.log_marks.get(name, 0), limit=limit)
        ]

    def record_failure(self, exc: BaseException, test_id: str) -> FailureReport | None:
        """Save the evidence of a failed test.

        Device logs go to `logs/<device>.jsonl`. An IntentViolation's report is completed
        (steps, environment, measurements, reproduction, commands; ADR-0005), written to
        `report.txt` / `report.json` and returned; any other failure returns None.
        """
        for name, dev in self.devices.items():
            mark = self.log_marks.get(name, 0)
            if dev.log.mark() > mark:
                dev.log.write_jsonl(self.artifacts / "logs" / f"{name}.jsonl", mark)
        if not isinstance(exc, IntentViolation):
            return None
        report = exc.report
        report.steps = self.step_lines()
        try:
            report.environment = self.backend.environment()
        except Exception as env_exc:  # noqa: BLE001
            report.environment = {"backend": self.backend.name, "env_error": str(env_exc)}
        report.measurements.update({m.check: m.value_s for m in self.measured()})
        report.snapshot_id = self.snapshot_id
        report.repro_command = (
            f"pytest '{test_id}' --backend {self.backend.name} --snapshot {self.snapshot_id}"
        )
        report.artifacts_dir = str(self.artifacts)
        report.commands = self.device_log_lines(limit=4)[-12:]
        report.write(self.artifacts)
        return report
