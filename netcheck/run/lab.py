"""The running lab as seen by tests: intent, devices, timing, cleanup, backend."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from netcheck.devices.device import Device
from netcheck.model.expected import Expected
from netcheck.model.intent import Intent
from netcheck.run.cleanup import CleanupRegistry
from netcheck.run.testrun import TestRun
from netcheck.run.timing import Timing

if TYPE_CHECKING:
    from netcheck.backends.base import Backend


@dataclass
class Lab:
    intent: Intent
    expected: Expected
    backend: Backend
    devices: dict[str, Device]
    timing: Timing
    artifacts_dir: Path = Path("artifacts")
    # the current test's state; before the first test, a run that covers the session
    run: TestRun = field(init=False)

    def __post_init__(self) -> None:
        self.run = TestRun(self.backend, self.devices, self.timing, self.artifacts_dir)

    def start_test(self, test_id: str, label: str) -> TestRun:
        """Begin a new test run: clear its artifacts, take its snapshot."""
        self.run = TestRun.begin(
            self.backend, self.devices, self.timing, self.artifacts_dir, test_id, label
        )
        return self.run

    @property
    def cleanup(self) -> CleanupRegistry:
        """The current test's cleanup registry (ADR-0003, layer 2)."""
        return self.run.cleanup

    @property
    def test_artifacts(self) -> Path:
        """The current test's artifacts folder."""
        return self.run.artifacts

    def __getitem__(self, name: str) -> Device:
        return self.devices[name]

    def by_role(self, role: str) -> list[Device]:
        return [d for d in self.devices.values() if d.role == role]


_current: contextvars.ContextVar[Lab] = contextvars.ContextVar("netcheck_lab")


def set_current(lab: Lab) -> contextvars.Token[Lab]:
    return _current.set(lab)


def current() -> Lab:
    try:
        return _current.get()
    except LookupError as exc:
        raise RuntimeError("no lab is active; use the `lab` fixture") from exc
