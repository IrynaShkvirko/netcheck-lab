"""Small persistent facts about the running lab, kept in .netcheck/ (ADR-0003).

- baseline: the snapshot taken right after a real deploy; `make lab-reset` restores it
- active faults: faults injected and not yet reset, shown in every pytest run header
- deployed intent: digest of the intent the running lab was built from
"""

from __future__ import annotations

import time
from pathlib import Path

DEFAULT_KEEP_SNAPSHOTS = 50


class LabState:
    def __init__(self, root: Path) -> None:
        self.dir = root / ".netcheck"
        self.snapshots = self.dir / "snapshots"
        self._baseline = self.dir / "baseline"
        self._faults = self.dir / "active-faults"
        self._deployed = self.dir / "deployed-intent"

    # baseline ---------------------------------------------------------------
    def baseline(self) -> str | None:
        try:
            value = self._baseline.read_text().strip()
        except FileNotFoundError:
            return None
        return value or None

    def set_baseline(self, snapshot_id: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._baseline.write_text(snapshot_id + "\n")

    # active faults ------------------------------------------------------------
    def active_faults(self) -> list[str]:
        try:
            return [ln for ln in self._faults.read_text().splitlines() if ln.strip()]
        except FileNotFoundError:
            return []

    def add_fault(self, name: str, description: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._faults.open("a") as fh:
            fh.write(f"{name}: {description} ({time.strftime('%Y-%m-%d %H:%M')})\n")

    def clear_faults(self) -> None:
        self._faults.unlink(missing_ok=True)

    # deployed intent ------------------------------------------------------------
    def deployed_intent(self) -> str | None:
        try:
            return self._deployed.read_text().strip() or None
        except FileNotFoundError:
            return None

    def set_deployed_intent(self, digest: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._deployed.write_text(digest + "\n")

    # snapshot pruning -------------------------------------------------------------
    def prune_snapshots(self, keep: int = DEFAULT_KEEP_SNAPSHOTS) -> list[Path]:
        """Delete all but the newest `keep` snapshots; never the baseline. Returns deleted."""
        if not self.snapshots.is_dir():
            return []
        protected = self.baseline()
        files = sorted(self.snapshots.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        doomed = [p for p in files[keep:] if p.stem != protected]
        for p in doomed:
            p.unlink(missing_ok=True)
        return doomed
