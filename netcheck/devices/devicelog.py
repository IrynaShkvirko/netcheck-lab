"""Per-device log of every command and API call (ADR-0002, ADR-0005)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LogEntry:
    ts: float
    device: str
    kind: str  # exec | api
    request: str
    response: Any
    rc: int | None
    duration_s: float


MAX_ENTRIES = 5000  # per device; a long session would otherwise grow without bound
MAX_RESPONSE_CHARS = 8000  # a full running-config is a few KB; huge API bodies get cut


def _trim(response: Any) -> Any:
    text = response if isinstance(response, str) else json.dumps(response, default=str)
    if len(text) <= MAX_RESPONSE_CHARS:
        return response
    return text[:MAX_RESPONSE_CHARS] + f"... [{len(text) - MAX_RESPONSE_CHARS} chars cut]"


@dataclass
class DeviceLog:
    device: str
    entries: list[LogEntry] = field(default_factory=list)
    max_entries: int = MAX_ENTRIES
    dropped: int = 0  # entries removed from the front; marks stay absolute

    def record(
        self, kind: str, request: str, response: Any, rc: int | None, started: float
    ) -> LogEntry:
        entry = LogEntry(
            ts=started,
            device=self.device,
            kind=kind,
            request=request,
            response=_trim(response),
            rc=rc,
            duration_s=round(time.time() - started, 4),
        )
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            cut = len(self.entries) - self.max_entries
            del self.entries[:cut]
            self.dropped += cut
        return entry

    def mark(self) -> int:
        """Position to slice from, so a test's log contains only its own calls."""
        return self.dropped + len(self.entries)

    def since(self, mark: int) -> list[LogEntry]:
        return self.entries[max(0, mark - self.dropped) :]

    def write_jsonl(self, path: Path, mark: int = 0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            for e in self.since(mark):
                fh.write(json.dumps(asdict(e), default=str) + "\n")

    def summary(self, mark: int = 0, limit: int = 15) -> list[str]:
        out = []
        for e in self.since(mark)[-limit:]:
            rc = "" if e.rc is None else f" rc={e.rc}"
            out.append(f"[{e.device}] {e.kind}: {e.request}{rc} ({e.duration_s}s)")
        return out
