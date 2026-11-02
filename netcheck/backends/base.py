"""Backend interface (ADR-0001, ADR-0003)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from netcheck.device import Device
from netcheck.intent import Intent


class Backend(ABC):
    name: str = "base"
    timing_profile: str = "local"

    def __init__(self, intent: Intent, settings: dict[str, Any], root: Path) -> None:
        self.intent = intent
        self.settings = settings
        self.root = root
        self.snapshot_dir = root / ".netcheck" / "snapshots"

    @abstractmethod
    def deploy(self, fresh: bool = False) -> bool:
        """Bring the lab up. Returns True if this call deployed it (False = reused)."""

    @abstractmethod
    def destroy(self) -> None: ...

    @abstractmethod
    def is_running(self) -> bool: ...

    @abstractmethod
    def devices(self) -> dict[str, Device]: ...

    @abstractmethod
    def snapshot(self, label: str) -> str:
        """Save configuration and link state; return the snapshot ID."""

    @abstractmethod
    def restore(self, snapshot_id: str) -> None: ...

    @abstractmethod
    def environment(self) -> dict[str, str]:
        """Topology, backend, NOS and image versions for reports (ADR-0005)."""
