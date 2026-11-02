from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from netcheck.intent import Intent, load_intent
from netcheck.lab import Lab, set_current

from .fakes import build_fake_lab

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def intent() -> Intent:
    return load_intent(ROOT / "intent" / "lab.yaml")


@pytest.fixture
def fake(intent: Intent, tmp_path: Path) -> Generator[tuple[Lab, dict[str, Any]], None, None]:
    lab, parts = build_fake_lab(intent, tmp_path)
    token = set_current(lab)
    yield lab, parts
    token.var.reset(token)
