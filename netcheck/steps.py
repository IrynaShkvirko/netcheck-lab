"""Named test steps.

    with step("Read the live VLAN membership from sw1"):
        live = sw1.bridge.vlan_membership()

Each step is numbered, logged as it starts (run pytest with
`-o log_cli=true --log-cli-level=INFO` to watch them live), and its outcome is kept,
so a failure report says which step failed and which ones passed before it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from netcheck.lab import StepRecord, current

log = logging.getLogger("netcheck.steps")


@contextmanager
def step(title: str) -> Iterator[StepRecord]:
    lab = current()
    record = StepRecord(number=len(lab.steps) + 1, title=title)
    lab.steps.append(record)
    log.info("step %d: %s", record.number, title)
    started = time.monotonic()
    try:
        yield record
    except BaseException:
        record.status = "failed"
        raise
    else:
        record.status = "passed"
    finally:
        record.seconds = time.monotonic() - started
        log.debug("step %d %s (%.1fs)", record.number, record.status, record.seconds)
