"""Command line: python -m netcheck <command>."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

from netcheck.backends import Backend, get_backend
from netcheck.backends.base import LabDegraded, RestoreFailed, SnapshotMismatch
from netcheck.faults import FAULTS
from netcheck.intent import load_intent

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="netcheck")
    p.add_argument("--intent", default=str(ROOT / "intent" / "lab.yaml"))
    p.add_argument("--backend", default="local", choices=["local", "nvidia"])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="generate topology, configs and expected state")
    d = sub.add_parser(
        "deploy", help="deploy the lab (or reuse it) and take a baseline after a real deploy"
    )
    d.add_argument("--fresh", action="store_true")
    sub.add_parser("destroy")
    sub.add_parser("snapshot")
    r = sub.add_parser("restore", help="restore a snapshot (default: baseline)")
    r.add_argument("snapshot_id", nargs="?")
    f = sub.add_parser("fault", help="inject a fault")
    f.add_argument("name", choices=sorted(FAULTS))
    c = sub.add_parser(
        "fault-check", help="inject each fault, prove the lab tests catch it, reset (lab up)"
    )
    c.add_argument(
        "names", nargs="*", metavar="FAULT", help=f"default: all ({', '.join(sorted(FAULTS))})"
    )
    s = sub.add_parser(
        "summary", help="this run's measurements next to earlier runs, as Markdown (for CI)"
    )
    s.add_argument("--artifacts", default=str(ROOT / "artifacts"))
    sub.add_parser(
        "record-fixtures",
        help="record the lab's real device output as unit-test samples (local backend, lab up)",
    )
    args = p.parse_args(argv)
    if args.cmd == "fault-check" and (unknown := sorted(set(args.names) - set(FAULTS))):
        p.error(f"unknown fault(s): {', '.join(unknown)}; choose from {', '.join(sorted(FAULTS))}")

    backend = get_backend(args.backend, load_intent(args.intent), ROOT)
    try:
        return _run(args, backend)
    except (LabDegraded, SnapshotMismatch, RestoreFailed, NotImplementedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, backend: Backend) -> int:
    state = backend.state
    if args.cmd == "render":
        print(f"rendered {backend.render()}")
    elif args.cmd == "deploy":
        had_baseline = state.baseline()
        deployed, baseline = backend.up(fresh=args.fresh)
        if deployed or baseline != had_baseline:
            print(
                f"{'deployed' if deployed else 'reused running lab'}; baseline snapshot {baseline}"
            )
        else:
            print(f"reused running lab; kept baseline {baseline}")
            faults = state.active_faults()
            if faults:
                print("warning: faults are still applied (run `make lab-reset`):")
                for line in faults:
                    print(f"  {line}")
    elif args.cmd == "destroy":
        backend.destroy()
    elif args.cmd == "snapshot":
        print(backend.snapshot("manual"))
    elif args.cmd == "restore":
        print(f"restored {backend.reset(args.snapshot_id)}")
    elif args.cmd == "fault":
        print(f"fault injected: {backend.inject_fault(args.name)}")
    elif args.cmd == "summary":
        import json

        from netcheck import measurements

        run = Path(args.artifacts) / measurements.RUN_FILE
        if not run.is_file():
            print(f"no measurements in {run}: no lab session measured anything")
            return 0
        history = ROOT / os.environ.get("NETCHECK_HISTORY", str(measurements.HISTORY_DEFAULT))
        current = json.loads(run.read_text())
        print(measurements.summary(current, measurements.load_history(history)), end="")
    elif args.cmd == "record-fixtures":
        if backend.name != "local":
            print("error: record-fixtures records the local backend; for NVIDIA use record_samples")
            return 1
        from netcheck.backends.local import LocalBackend
        from netcheck.backends.local_samples import NotConverged, record_local_samples

        dest = ROOT / "tests" / "unit" / "fixtures" / "local"
        from netcheck.timing import Timing

        intent = backend.intent
        timing = Timing.load(ROOT / "timing.yaml", backend.timing_profile, intent.timer_vars())
        try:
            written = record_local_samples(cast(LocalBackend, backend), dest, timing)
        except NotConverged as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        where = dest.relative_to(ROOT)
        print(f"recorded {len(written)} devices into {where} (provenance.yaml updated)")
        print("review `git diff tests/unit/fixtures/local`, then run `make check`")
    elif args.cmd == "fault-check":
        from netcheck.faultcheck import check_faults

        pytest_cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
        pytest_cmd += ["--backend", backend.name, "--intent", args.intent]
        artifacts = ROOT / "artifacts" / "fault-check"
        # faulted sessions measure a broken lab: keep them out of the measurement history
        env = {**os.environ, "NETCHECK_HISTORY": str(artifacts / "history.jsonl")}
        ok = check_faults(
            backend,
            pytest_cmd,
            artifacts,
            args.names,
            run=lambda cmd: subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode,
        )
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
