"""The intents unit tests render and check: the lab's own, plus test-only shapes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# intent/lab.yaml is deployed by CI; tests/unit/intents/*.yaml exist only to give the
# renderer and Expected a second shape (three routers, two switches, other timers).
INTENT_FILES: list[Path] = [
    ROOT / "intent" / "lab.yaml",
    *sorted((ROOT / "tests" / "unit" / "intents").glob("*.yaml")),
]


def intent_id(path: Path) -> str:
    """Test ID for an intent file: lab.yaml -> lab, two-site.yaml -> two-site."""
    return path.stem
