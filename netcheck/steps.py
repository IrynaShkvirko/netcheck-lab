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
(`TestRun.soft_failure_report`). Only check failures (AssertionError, which includes
IntentViolation) continue; any other error, pytest.fail and pytest.skip still stop the
test, because the steps after them could not be trusted. The idea comes from pyATS
(`steps.start(..., continue_=True)`); see ADR-0006.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from netcheck.lab import current
from netcheck.testrun import StepRecord, failure_summary

log = logging.getLogger("netcheck.steps")


@contextmanager
def step(title: str, *, continue_on_failure: bool = False) -> Iterator[StepRecord]:
    steps = current().run.steps
    record = StepRecord(number=len(steps) + 1, title=title, soft=continue_on_failure)
    steps.append(record)
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
