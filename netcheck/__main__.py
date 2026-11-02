"""Command line: python -m netcheck <command>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from netcheck.backends import get_backend
from netcheck.faults import FAULTS
from netcheck.intent import load_intent
from netcheck.render import render

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".netcheck" / "baseline"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="netcheck")
    p.add_argument("--intent", default=str(ROOT / "intent" / "lab.yaml"))
    p.add_argument("--backend", default="local", choices=["local", "nvidia"])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="generate topology, configs and expected state")
    d = sub.add_parser("deploy", help="deploy the lab and take a baseline snapshot")
    d.add_argument("--fresh", action="store_true")
    sub.add_parser("destroy")
    sub.add_parser("snapshot")
    r = sub.add_parser("restore", help="restore a snapshot (default: baseline)")
    r.add_argument("snapshot_id", nargs="?")
    f = sub.add_parser("fault", help="inject a demo fault")
    f.add_argument("name", choices=sorted(FAULTS))
    args = p.parse_args(argv)

    intent = load_intent(args.intent)
    if args.cmd == "render":
        settings = yaml.safe_load((ROOT / "backends.yaml").read_text())["local"]
        out = render(intent, settings["images"], ROOT / "build" / intent.name)
        print(f"rendered {out}")
        return 0

    backend = get_backend(args.backend, intent, ROOT)
    if args.cmd == "deploy":
        deployed = backend.deploy(fresh=args.fresh)
        snap = backend.snapshot("baseline")
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(snap)
        print(f"{'deployed' if deployed else 'reused running lab'}; baseline snapshot {snap}")
    elif args.cmd == "destroy":
        backend.destroy()
    elif args.cmd == "snapshot":
        print(backend.snapshot("manual"))
    elif args.cmd == "restore":
        snap = args.snapshot_id or BASELINE.read_text().strip()
        backend.restore(snap)
        print(f"restored {snap}")
    elif args.cmd == "fault":
        print(f"fault injected: {FAULTS[args.name](backend)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
