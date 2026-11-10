from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from netcheck import provenance
from netcheck.intent import Intent, load_intent
from netcheck.lab import Lab, set_current

from .fakes import build_fake_lab

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Say in every unit run how much of the device-facing code ran on real output."""
    status = provenance.status(FIXTURES / "nvue")
    line = f"netcheck samples: {status.line('NVUE')}"
    if not status.verified:
        line += ": the NVIDIA parsers are unverified"
    lines = [line]
    if (FIXTURES / "local" / provenance.FILE).is_file():
        local = provenance.status(FIXTURES / "local")
        lines.append(f"netcheck samples: {local.line('local FRR/iproute2')} (make record-fixtures)")
    return lines


def pytest_terminal_summary(terminalreporter: Any, config: pytest.Config) -> None:
    """Count the tests that ran (passed or failed) on documentation samples only."""
    ran = [
        r
        for outcome in ("passed", "failed")
        for r in terminalreporter.stats.get(outcome, [])
        if getattr(r, "when", "") == "call" and "documentation_samples" in r.keywords
    ]
    if ran:
        terminalreporter.write_line(
            f"netcheck: {len(ran)} tests ran on documentation samples only "
            "(marker documentation_samples): passing them does not show the device side works"
        )


@pytest.fixture
def intent() -> Intent:
    return load_intent(ROOT / "intent" / "lab.yaml")


@pytest.fixture
def fake(intent: Intent, tmp_path: Path) -> Generator[tuple[Lab, dict[str, Any]], None, None]:
    lab, parts = build_fake_lab(intent, tmp_path)
    token = set_current(lab)
    yield lab, parts
    token.var.reset(token)
