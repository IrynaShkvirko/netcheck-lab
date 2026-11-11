"""Where each test sample came from.

Unit tests run the parsers on device responses kept as files under tests/unit/fixtures/.
A sample is either `captured` (recorded from a running device by `record_samples` or
`make record-fixtures`) or `documentation` (written by hand from the NOS documentation).
A parser that passes only on documentation samples is unverified: its author, the sample
and the test share one belief about the device. Each fixture folder says which is which in
`provenance.yaml`, and the test headers, the README and the agents read it from there:

    note: ...
    samples:
      r1/interfaces.applied.json: {source: documentation, nos: Cumulus Linux 5.x, date: ...}
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

FILE = "provenance.yaml"
SOURCES = ("captured", "documentation")
SAMPLE_SUFFIXES = (".json", ".txt")
NOTE = (
    "How each sample here was made. captured: recorded from a running device. "
    "documentation: written by hand from the NOS documentation; a parser tested only "
    "on these is unverified."
)


class ProvenanceError(ValueError):
    """provenance.yaml is missing, malformed, or does not match the sample files."""


@dataclass(frozen=True)
class Sample:
    path: str  # relative to the fixture folder: r1/interfaces.applied.json
    source: str  # captured | documentation
    nos: str  # what produced (or was assumed to produce) it
    date: str  # YYYY-MM-DD


@dataclass(frozen=True)
class Status:
    captured: int
    total: int

    @property
    def verified(self) -> bool:
        return self.total > 0 and self.captured == self.total

    def line(self, what: str) -> str:
        """'0 of 10 NVUE samples captured from a device'."""
        return f"{self.captured} of {self.total} {what} samples captured from a device"


def sample_files(folder: Path) -> list[str]:
    """Every sample file in the folder, relative to it (README and provenance excluded)."""
    return sorted(
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file() and p.suffix in SAMPLE_SUFFIXES
    )


def load(folder: Path) -> list[Sample]:
    """The folder's samples, checked: every file listed, every listed file present, every
    source one of SOURCES. Raises ProvenanceError with every problem found."""
    path = folder / FILE
    if not path.is_file():
        raise ProvenanceError(f"{path} is missing: say how each sample was made")
    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("samples") or {}
    problems = []
    samples = []
    for rel, info in sorted(entries.items()):
        info = info or {}
        source = info.get("source")
        if source not in SOURCES:
            problems.append(f"{rel}: source {source!r} is not one of {', '.join(SOURCES)}")
        samples.append(
            Sample(rel, str(source), str(info.get("nos", "")), str(info.get("date", "")))
        )
    files = set(sample_files(folder))
    listed = set(entries)
    problems += [f"{rel}: sample file not listed in {FILE}" for rel in sorted(files - listed)]
    problems += [f"{rel}: listed in {FILE} but missing" for rel in sorted(listed - files)]
    if problems:
        raise ProvenanceError(f"{path}: " + "; ".join(problems))
    return samples


def status(folder: Path) -> Status:
    samples = load(folder)
    return Status(sum(1 for s in samples if s.source == "captured"), len(samples))


def mark_captured(folder: Path, files: Iterable[Path], nos: str, date: str = "") -> None:
    """Record `files` (inside `folder`) as captured from `nos` today, keeping the other
    entries. Called by the recorders after they write samples."""
    path = folder / FILE
    raw = (yaml.safe_load(path.read_text()) if path.is_file() else None) or {}
    entries = dict(raw.get("samples") or {})
    day = date or dt.date.today().isoformat()
    for f in files:
        rel = Path(f).resolve().relative_to(folder.resolve()).as_posix()
        entries[rel] = {"source": "captured", "nos": nos, "date": day}
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"note": NOTE, "samples": dict(sorted(entries.items()))}, sort_keys=False)
    )
