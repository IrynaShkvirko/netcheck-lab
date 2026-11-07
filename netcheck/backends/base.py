"""Backend interface (ADR-0001, ADR-0003)."""

from __future__ import annotations

import json
import secrets
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from netcheck.capabilities import Links, Snapshot
from netcheck.device import Device
from netcheck.intent import Intent
from netcheck.labstate import LabState

# Version of the snapshot file layout; older files are refused (ADR-0003).
SNAPSHOT_FORMAT = 2


class LabDegraded(RuntimeError):
    """The lab is running but not usable as deployed (e.g. links lost); redeploy needed."""


class SnapshotMismatch(ValueError):
    """The snapshot cannot be restored onto this lab: unknown ID, older file format,
    another intent or another backend. Nothing was changed."""


class RestoreFailed(RuntimeError):
    """One or more devices could not be brought back to the snapshot."""

    def __init__(self, snapshot_id: str, errors: dict[str, str]) -> None:
        self.snapshot_id = snapshot_id
        self.errors = errors
        listing = "; ".join(f"{name}: {err}" for name, err in errors.items())
        super().__init__(f"restore of {snapshot_id} failed on {len(errors)} device(s): {listing}")


class Backend(ABC):
    name: str = "base"
    timing_profile: str = "local"

    def __init__(self, intent: Intent, settings: dict[str, Any], root: Path) -> None:
        self.intent = intent
        self.settings = settings
        self.root = root
        self.state = LabState(root)
        self.snapshot_dir = self.state.snapshots

    def up(self, fresh: bool = False) -> tuple[bool, str]:
        """Deploy or reuse the lab and make sure a trustworthy baseline exists.

        A running lab is reused only if it was built from this intent and still has
        every intended port; otherwise (or with `fresh`) it is deployed again and the
        deployed intent recorded. A baseline is taken only right after a real deploy (or
        if none exists yet), never from a reused lab, which may still carry an injected
        fault. Returns (deployed, baseline snapshot ID).
        """
        deployed = self._deploy_unless_reusable(fresh)
        baseline = self.state.baseline()
        if deployed or baseline is None:
            baseline = self.snapshot("baseline")
            self.state.set_baseline(baseline)
            self.state.clear_faults()
        return deployed, baseline

    def _deploy_unless_reusable(self, fresh: bool) -> bool:
        """Reuse the running lab if it can be trusted, else deploy. True if deployed."""
        if self.is_running() and not fresh:
            built_from = self.state.deployed_intent()
            missing = self.missing_ports()
            if built_from == self.intent.digest and not missing:
                return False
            if missing:
                print(f"lab is running but degraded (missing {', '.join(missing)}); redeploying")
            else:
                print(
                    f"lab was built from intent {built_from or 'unknown'}, "
                    f"intent is now {self.intent.digest}; redeploying"
                )
        self.deploy()
        self.state.set_deployed_intent(self.intent.digest)
        return True

    def reset(self, snapshot_id: str | None = None) -> str:
        """Restore a snapshot, the baseline by default; returns the restored ID.

        Restoring the baseline clears the fault ledger, whoever asks (CLI or pytest):
        the baseline has no faults in it. Another snapshot may have been taken with a
        fault applied, so restoring it leaves the ledger as it is.
        """
        baseline = self.state.baseline()
        target = snapshot_id or baseline
        if target is None:
            raise SnapshotMismatch("no baseline yet; run `make lab-up` first")
        self.restore(target)
        if target == baseline:
            self.state.clear_faults()
        return target

    def inject_fault(self, name: str) -> str:
        """Break one intent on purpose and record it in the fault ledger; returns what
        was changed. `reset()` to the baseline removes it again."""
        from netcheck.faults import FAULTS  # faults drive a Backend: import late

        description = FAULTS[name](self)
        self.state.add_fault(name, description)
        return description

    def render(self) -> Path:
        """Generate the lab from the intent (topology, configs, expected state); returns
        the topology file."""
        raise NotImplementedError(f"the {self.name} backend cannot generate a topology yet")

    @abstractmethod
    def deploy(self) -> None:
        """Build the lab now, replacing a running one. `up()` decides when."""

    @abstractmethod
    def destroy(self) -> None: ...

    @abstractmethod
    def is_running(self) -> bool:
        """Every node of the lab is up (it may still be degraded or from another intent)."""

    @abstractmethod
    def devices(self) -> dict[str, Device]: ...

    def missing_ports(self) -> list[str]:
        """Intended ports that do not exist on their device, e.g. ["r1:p1 (eth1)"].

        Empty means the lab is intact. A container lab loses its links when a container
        restarts; such a lab must be redeployed, not restored.
        """
        missing = []
        for name, dev in self.devices().items():
            if not dev.has(Links):
                continue
            present = dev.links.link_states(list(dev.port_map))
            missing += [
                f"{name}:{lp} ({real})" for lp, real in dev.port_map.items() if lp not in present
            ]
        return missing

    # snapshots (ADR-0003) ----------------------------------------------------
    def snapshot(self, label: str) -> str:
        """Save every device's state into one file; return the snapshot ID."""
        snap_id = f"{label}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        data: dict[str, Any] = {
            "format": SNAPSHOT_FORMAT,
            "id": snap_id,
            "intent": self.intent.digest,
            "backend": self.name,
            "devices": {},
            "not_covered": [],
        }
        for name, dev in self.devices().items():
            if dev.has(Snapshot):
                data["devices"][name] = dev.snapshot.take()
            else:
                data["not_covered"].append(name)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        (self.snapshot_dir / f"{snap_id}.json").write_text(json.dumps(data, indent=2))
        self.state.prune_snapshots()
        return snap_id

    def restore(self, snapshot_id: str) -> None:
        """Bring every device back to the snapshot.

        Raises SnapshotMismatch (nothing changed) if the snapshot is not from this lab,
        LabDegraded if the lab must be redeployed, RestoreFailed listing every device
        that did not come back.
        """
        data = self._load_snapshot(snapshot_id)
        missing = self.missing_ports()
        if missing:
            raise LabDegraded(
                f"cannot restore {snapshot_id}: lab links are gone ({', '.join(missing)}). "
                "A container was probably restarted. Run `make lab-fresh`."
            )
        devices = self.devices()
        errors: dict[str, str] = {}
        for name, state in data["devices"].items():
            try:
                devices[name].snapshot.restore_to(state)
            except Exception as exc:  # noqa: BLE001
                errors[name] = f"{type(exc).__name__}: {exc}"
        if errors:
            raise RestoreFailed(snapshot_id, errors)

    def _load_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        path = self.snapshot_dir / f"{snapshot_id}.json"
        try:
            data: dict[str, Any] = json.loads(path.read_text())
        except FileNotFoundError:
            raise SnapshotMismatch(f"no snapshot {snapshot_id} in {self.snapshot_dir}") from None
        if data.get("format") != SNAPSHOT_FORMAT:
            raise SnapshotMismatch(
                f"snapshot {snapshot_id} has an older file format; "
                "take a new baseline with `make lab-fresh`"
            )
        if data.get("backend") != self.name:
            raise SnapshotMismatch(
                f"snapshot {snapshot_id} was taken on backend '{data.get('backend')}', "
                f"not '{self.name}'"
            )
        if data.get("intent") != self.intent.digest:
            raise SnapshotMismatch(
                f"snapshot {snapshot_id} was taken from intent {data.get('intent')}, "
                f"but the lab is built from {self.intent.digest}; restore a snapshot of "
                "this intent or redeploy with `make lab-fresh`"
            )
        return data

    @abstractmethod
    def environment(self) -> dict[str, str]:
        """Topology, backend, NOS and image versions for reports (ADR-0005)."""
