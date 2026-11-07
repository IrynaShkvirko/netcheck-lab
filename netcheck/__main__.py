"""Command line: python -m netcheck <command>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
    args = p.parse_args(argv)

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
