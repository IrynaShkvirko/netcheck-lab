"""Command line: python -m netcheck <command>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from netcheck.backends import Backend, get_backend
from netcheck.backends.base import LabDegraded
from netcheck.faults import FAULTS
from netcheck.intent import load_intent
from netcheck.labstate import LabState
from netcheck.render import render

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

    intent = load_intent(args.intent)
    if args.cmd == "render":
        settings = yaml.safe_load((ROOT / "backends.yaml").read_text())["local"]
        out = render(intent, settings["images"], ROOT / "build" / intent.name)
        print(f"rendered {out}")
        return 0

    backend = get_backend(args.backend, intent, ROOT)
    try:
        return _run(args, backend, LabState(ROOT))
    except LabDegraded as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, backend: Backend, state: LabState) -> int:
    if args.cmd == "deploy":
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
        snap = args.snapshot_id or state.baseline()
        if snap is None:
            print("error: no baseline yet; run `make lab-up` first", file=sys.stderr)
            return 1
        backend.restore(snap)
        if args.snapshot_id is None:
            state.clear_faults()  # the baseline has no faults in it
        print(f"restored {snap}")
    elif args.cmd == "fault":
        description = FAULTS[args.name](backend)
        state.add_fault(args.name, description)
        print(f"fault injected: {description}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
