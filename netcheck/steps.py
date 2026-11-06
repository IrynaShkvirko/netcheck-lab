"""Named test steps.

    with step("Read the live VLAN membership from sw1"):
        live = sw1.bridge.vlan_membership()

Each step is numbered, logged as it starts (run pytest with
`-o log_cli=true --log-cli-level=INFO` to watch them live), and its outcome is kept,
so a failure report says which step failed and which ones passed before it.

Soft steps (`continue_on_failure=True`) are for independent checks of the same kind,
such as one step per switch port. A failed check is recorded and the test goes on, so a
single run reports every bad port instead of stopping at the first. When the test body
ends, the pytest plugin fails the test with one report that combines all of them
(`soft_failure_report`). Only check failures (AssertionError, which includes
IntentViolation) continue; any other error, pytest.fail and pytest.skip still stop the
test, because the steps after them could not be trusted. The idea comes from pyATS
(`steps.start(..., continue_=True)`); see ADR-0006.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from netcheck.lab import StepRecord, current, failure_summary
from netcheck.report import FailureReport, IntentViolation

log = logging.getLogger("netcheck.steps")


@contextmanager
def step(title: str, *, continue_on_failure: bool = False) -> Iterator[StepRecord]:
    lab = current()
    record = StepRecord(number=len(lab.steps) + 1, title=title, soft=continue_on_failure)
    lab.steps.append(record)
    log.info("step %d: %s", record.number, title)
    started = time.monotonic()
    try:
        yield record
    except AssertionError as exc:
        record.status = "failed"
        if not continue_on_failure:
            raise
        record.failure = exc  # not re-raised: the test continues with the next step
        log.warning("step %d failed, continuing: %s", record.number, failure_summary(exc))
    except BaseException:
        record.status = "failed"
        raise
    else:
        record.status = "passed"
    finally:
        record.seconds = time.monotonic() - started
        log.debug("step %d %s (%.1fs)", record.number, record.status, record.seconds)


def soft_failures(steps: Sequence[StepRecord]) -> list[StepRecord]:
    return [s for s in steps if s.failure is not None]


def soft_failure_report(steps: Sequence[StepRecord]) -> FailureReport | None:
    """One report for all soft steps that failed, or None if none did.

    Diffs, drift and captures from each failure are kept. Evidence is merged by title;
    a title already present with the same lines (the same switch read twice) is not
    repeated, and one with different lines gets the step number added. A plain `assert`
    failure becomes an evidence entry named after its step.
    """
    failed = soft_failures(steps)
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
