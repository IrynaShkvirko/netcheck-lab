"""Cleanup registry (ADR-0003, layer 2).

Register the undo action at the moment a resource is created. Actions run in
reverse order (LIFO) when the test ends, also when it fails. A failing undo is
recorded but never stops the other undos or hides the test's own failure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("netcheck.cleanup")


@dataclass
class CleanupError:
    description: str
    error: str


@dataclass
class _Action:
    description: str
    fn: Callable[[], object]


@dataclass
class CleanupRegistry:
    _stack: list[_Action] = field(default_factory=list)

    def register(self, description: str, fn: Callable[[], object]) -> None:
        self._stack.append(_Action(description, fn))

    def __len__(self) -> int:
        return len(self._stack)

    def run(self) -> list[CleanupError]:
        """Run every registered undo, newest first. Returns errors from this run."""
        run_errors: list[CleanupError] = []
        while self._stack:
            action = self._stack.pop()
            try:
                action.fn()
                log.debug("cleanup ok: %s", action.description)
            except Exception as exc:  # noqa: BLE001 - every undo must get its chance
                err = CleanupError(action.description, f"{type(exc).__name__}: {exc}")
                run_errors.append(err)
                log.warning("cleanup failed: %s (%s)", action.description, err.error)
        return run_errors
