"""The numbers a lab session measured, for this run and over time (ADR-0004).

At the end of every lab session the pytest plugin writes `artifacts/measurements.json` (this
run) and appends the same record as one line to the history file (`NETCHECK_HISTORY`,
default `.netcheck/measurements.jsonl`). `python -m netcheck summary` prints this run's
numbers next to the median of earlier runs on the same backend and intent, as Markdown: CI
puts it in the job summary, so a slow drift is visible before it breaks an SLA.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from netcheck.run.timing import Measurement

RUN_FILE = "measurements.json"
HISTORY_DEFAULT = Path(".netcheck") / "measurements.jsonl"
HISTORY_RUNS = 30  # earlier runs a summary compares against


def run_record(
    measured: Iterable[tuple[str, Measurement]], context: dict[str, Any]
) -> dict[str, Any]:
    """One session's measurements, with what they were measured on (backend, intent, ...)."""
    return {
        "time": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        **context,
        "measurements": [
            {
                "test": test.split("::")[-1],
                "check": m.check,
                "value_s": m.value_s,
                "sla_s": m.sla_s,
                "breached": m.breached,
            }
            for test, m in measured
        ],
    }


def save(record: dict[str, Any], artifacts: Path, history: Path) -> None:
    """Write this run's file and append it to the history."""
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / RUN_FILE).write_text(json.dumps(record, indent=2) + "\n")
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def load_history(history: Path) -> list[dict[str, Any]]:
    if not history.is_file():
        return []
    return [json.loads(line) for line in history.read_text().splitlines() if line.strip()]


def _values(record: dict[str, Any]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for m in record.get("measurements", []):
        out.setdefault(m["check"], []).append(m["value_s"])
    return out


def summary(current: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """Markdown: each check of this run (its median when measured several times), its SLA,
    and the median of the same check over the earlier runs on the same backend and intent."""
    same = [
        r
        for r in history
        if r.get("backend") == current.get("backend")
        and r.get("intent") == current.get("intent")
        and r.get("time") != current.get("time")
    ][-HISTORY_RUNS:]
    earlier: dict[str, list[float]] = {}
    for r in same:
        for check, values in _values(r).items():
            earlier.setdefault(check, []).append(statistics.median(values))
    slas = {m["check"]: m["sla_s"] for m in current.get("measurements", [])}
    breached = {m["check"] for m in current.get("measurements", []) if m["breached"]}
    lines = [
        f"### netcheck measurements ({current.get('backend')}, intent {current.get('intent')}, "
        f"SLA mode {current.get('sla_mode')}, repeats {current.get('repeats')})",
        "",
        "| Check | This run (s) | SLA (s) | Earlier runs, median (s) | Change |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check, values in sorted(_values(current).items()):
        value = statistics.median(values)
        shown = f"{value:.3f}" + (f" (median of {len(values)})" if len(values) > 1 else "")
        if check in breached:
            shown += " **SLA breach**"
        sla = slas.get(check)
        before = earlier.get(check)
        if before:
            base = statistics.median(before)
            change = f"{(value - base) / base:+.0%}" if base else "n/a"
            past = f"{base:.3f} ({len(before)} runs)"
        else:
            past, change = "first run", ""
        lines.append(
            f"| {check} | {shown} | {'' if sla is None else f'{sla:g}'} | {past} | {change} |"
        )
    if len(lines) == 4:
        lines.append("| (no measurements) | | | | |")
    return "\n".join(lines) + "\n"
