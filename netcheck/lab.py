"""The running lab as seen by tests: intent, devices, timing, cleanup, backend."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from netcheck.cleanup import CleanupRegistry
from netcheck.device import Device
from netcheck.intent import Intent
from netcheck.timing import Timing

if TYPE_CHECKING:
    from netcheck.backends.base import Backend


@dataclass
class Lab:
    intent: Intent
    expected: dict[str, Any]
    backend: Backend
    devices: dict[str, Device]
    timing: Timing
    cleanup: CleanupRegistry = field(default_factory=CleanupRegistry)
    artifacts_dir: Path = Path("artifacts")
    # per-test context, set by fixtures
    test_artifacts: Path = Path("artifacts")
    snapshot_id: str = ""
    log_marks: dict[str, int] = field(default_factory=dict)

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
