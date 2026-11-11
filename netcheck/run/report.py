"""Eight-part failure report (ADR-0005)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DiffLine:
    """One expected-vs-actual difference."""

    where: str
    expected: Any
    actual: Any
    note: str = ""


@dataclass
class FailureReport:
    # 1. verdict
    verdict: str
    # test steps run so far, filled by the pytest plugin ("which step failed")
    steps: list[str] = field(default_factory=list)
    # 2. expected vs actual
    expected: str = ""
    actual: str = ""
    state_diff: list[DiffLine] = field(default_factory=list)
    # 3. discriminating evidence
    evidence: dict[str, list[str]] = field(default_factory=dict)
    # 4. intent vs live config
    drift: list[DiffLine] = field(default_factory=list)
    # 5. commands / API calls (summary; full log in artifacts)
    commands: list[str] = field(default_factory=list)
    # 6. packet capture
    capture_summary: list[str] = field(default_factory=list)
    capture_files: list[str] = field(default_factory=list)
    # 7. environment
    environment: dict[str, str] = field(default_factory=dict)
    measurements: dict[str, float] = field(default_factory=dict)
    # 8. reproduction
    repro_command: str = ""
    snapshot_id: str = ""
    artifacts_dir: str = ""

    def add_evidence(self, title: str, lines: list[str]) -> None:
        self.evidence.setdefault(title, []).extend(lines)

    # ---- rendering -----------------------------------------------------
    def render(self) -> str:
        out: list[str] = [f"INTENT VIOLATED: {self.verdict}", ""]
        if self.steps:
            out.append("Steps:")
            out += [f"  {s}" for s in self.steps]
            out.append("")
        if self.expected or self.actual:
            out += [f"Expected: {self.expected}", f"Actual:   {self.actual}", ""]
        if self.state_diff:
            out.append("State diff:")
            out += [_diff(d) for d in self.state_diff]
            out.append("")
        if self.drift:
            out.append("Intent vs live:")
            out += [
                _diff(d) + ("   <-- drift" if d.expected != d.actual else "") for d in self.drift
            ]
            out.append("")
        if self.evidence:
            out.append("Evidence:")
            for title, lines in self.evidence.items():
                out.append(f"  {title}:")
                out += [f"    {ln}" for ln in lines]
            out.append("")
        if self.capture_summary:
            out.append("Capture (summary):")
            out += [f"  {ln}" for ln in self.capture_summary]
            out += [f"  file: {f}" for f in self.capture_files]
            out.append("")
        if self.commands:
            out.append("Commands run (last calls; full log in artifacts):")
            out += [f"  {c}" for c in self.commands]
            out.append("")
        if self.environment or self.measurements:
            env = "  ".join(f"{k}={v}" for k, v in self.environment.items())
            out.append(f"Env: {env}")
            if self.measurements:
                meas = "  ".join(f"{k}={v}s" for k, v in self.measurements.items())
                out.append(f"Measured: {meas}")
        if self.repro_command:
            out.append(f"Repro: {self.repro_command}")
        if self.snapshot_id:
            out.append(f"Restore state: python -m netcheck restore {self.snapshot_id}")
        if self.artifacts_dir:
            out.append(f"Artifacts: {self.artifacts_dir}")
        return "\n".join(out).rstrip() + "\n"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.txt").write_text(self.render())
        (directory / "report.json").write_text(self.to_json())


def _diff(d: DiffLine) -> str:
    note = f"  ({d.note})" if d.note else ""
    return f"  {d.where}: expected {d.expected!r}, actual {d.actual!r}{note}"


class IntentViolation(AssertionError):
    """Raised by domain assertions. Carries the report; the test run completes it."""

    def __init__(self, report: FailureReport) -> None:
        super().__init__(report.verdict)
        self.report = report

    def __str__(self) -> str:
        return self.report.render()
