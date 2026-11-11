"""Backend registry."""

from __future__ import annotations

from pathlib import Path

import yaml

from netcheck.backends.base import Backend
from netcheck.model.intent import Intent


def get_backend(name: str, intent: Intent, root: Path) -> Backend:
    settings = yaml.safe_load((root / "backends.yaml").read_text())
    if name == "local":
        from netcheck.backends.local import LocalBackend

        return LocalBackend(intent, settings["local"], root)
    if name == "nvidia":
        from netcheck.backends.nvidia_air import NvidiaAirBackend

        return NvidiaAirBackend(intent, settings["nvidia"], root)
    raise ValueError(f"unknown backend {name!r}; use local or nvidia")


__all__ = ["Backend", "get_backend"]
