#!/usr/bin/env python3
"""The mutation floor: hold the guarded modules to a minimum mutation score, and make every
survivor in them either killed or accepted with a reason. The floors and the accepted
survivors are in pyproject.toml, [tool.mutation-floor] and [tool.mutation-accepted].

    mutation-floor.py check [--modules M ...]   read mutmut's results (mutants/), exit 1 on a breach
    mutation-floor.py select < changed-paths    the guarded modules a change must mutate

`check` reads the `.meta` files mutmut writes next to each mutated copy, so it needs no
mutmut import and runs after `mutmut run` (full, or only some modules). A module's score is
killed / all its mutants: a survivor and a mutant no unit test reaches both count against
it, accepted or not, so accepting a survivor never raises the score. Three rules, per
guarded module:

1. every mutant was checked (a partial run cannot pass for a full one);
2. the score is at least the module's floor;
3. every survivor and no-tests mutant matches an [accepted] entry, and every entry still
   matches one (a stale reason is removed, not left to excuse the next survivor).

`select` decides what a pull request must mutate: the guarded modules it changed, or all of
them when it changed the unit tests or the mutmut config (a weaker test lowers the score
of code it did not touch), or when there is no list (a manual run). Nothing when it changed
neither; the nightly run does the rest. Prints `modules=` and `globs=` for $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG = Path("pyproject.toml")
MUTANTS = Path("mutants")

# mutmut's exit codes (mutmut/stats.py, status_by_exit_code), reduced to what the floor needs
KILLED = {1, 3, 36, 37, -24, 24, 152, 255}  # failed test, pytest error, timeout, type check
SKIPPED = {34}
NOT_CHECKED = {None}

ALL_TESTS = ("tests/unit/", "conftest.py", "pyproject.toml")


@dataclass
class Module:
    name: str
    killed: int = 0
    total: int = 0
    not_checked: int = 0
    unkilled: list[str] = field(default_factory=list)  # survived or no tests, by name

    @property
    def score(self) -> float:
        return 100.0 * self.killed / self.total if self.total else 100.0


def load_config(path: Path = CONFIG) -> tuple[dict[str, float], dict[str, str]]:
    """Floors by module, and accepted survivors as {mutant name or pattern: reason}. The
    file groups them by reason: `"<reason>" = ["<name>", ...]`."""
    tool = tomllib.loads(path.read_text())["tool"]
    floors = {m: float(v) for m, v in tool["mutation-floor"].items()}
    accepted: dict[str, str] = {}
    for reason, patterns in tool.get("mutation-accepted", {}).items():
        if not reason.strip():
            raise SystemExit(f"{path}: accepted without a reason: {patterns}")
        for pattern in patterns:
            if pattern in accepted:
                raise SystemExit(f"{path}: {pattern} is accepted twice")
            accepted[pattern] = reason.strip()
    return floors, accepted


def read_results(mutants: Path = MUTANTS) -> dict[str, Module]:
    """Mutant statuses from mutmut's .meta files, grouped by module."""
    modules: dict[str, Module] = {}
    for meta in sorted(mutants.rglob("*.py.meta")):
        for name, code in json.loads(meta.read_text())["exit_code_by_key"].items():
            mod = modules.setdefault(name.rpartition(".")[0], Module(name.rpartition(".")[0]))
            if code in SKIPPED:
                continue
            mod.total += 1
            if code in NOT_CHECKED:
                mod.not_checked += 1
            elif code in KILLED:
                mod.killed += 1
            else:  # survived, no tests; suspicious and segfault are not kills either
                mod.unkilled.append(name)
    return modules


def check(
    floors: dict[str, float], accepted: dict[str, str], results: dict[str, Module], only: list[str]
) -> tuple[list[str], list[str]]:
    """Breaches and notes for the guarded modules (all of them, or `only`)."""
    breaches, notes = [], []
    guarded = [m for m in floors if not only or m in only]
    if unknown := [m for m in only if m not in floors]:
        breaches.append(
            f"not guarded modules: {unknown} (add a floor to [tool.mutation-floor] in {CONFIG})"
        )
    used: set[str] = set()
    for name in guarded:
        mod = results.get(name)
        if mod is None or mod.total == 0:
            breaches.append(f'{name}: no mutmut results (run `mutmut run "{name}.*"`)')
            continue
        if mod.not_checked:
            breaches.append(f"{name}: {mod.not_checked} of {mod.total} mutants not checked")
            continue
        line = (
            f"{name}: {mod.score:.1f}% killed ({mod.killed}/{mod.total}), floor {floors[name]:g}%"
        )
        if mod.score < floors[name]:
            breaches.append(f"{line}: BELOW THE FLOOR")
        elif mod.score >= floors[name] + 2:
            notes.append(f"{line}: raise the floor to {int(mod.score)}")
        else:
            notes.append(line)
        for mutant in mod.unkilled:
            match = [p for p in accepted if fnmatch.fnmatchcase(mutant, p)]
            used.update(match)
            if not match:
                breaches.append(f"{name}: survivor not accepted: {mutant}")
    checked = {m for m in guarded if m in results and not results[m].not_checked}
    for pattern in accepted:
        owner = next((m for m in floors if pattern.startswith(m + ".")), None)
        if owner is None:
            breaches.append(f"accepted entry outside the guarded modules: {pattern}")
        elif owner in checked and pattern not in used:
            breaches.append(f"accepted entry matches no survivor (remove it): {pattern}")
    return breaches, notes


def select(paths: list[str], floors: dict[str, float]) -> list[str]:
    """The guarded modules a change can weaken."""
    paths = [p.strip() for p in paths if p.strip()]
    if not paths or any(p.startswith(ALL_TESTS) for p in paths):
        return list(floors)
    changed = {p.removesuffix(".py").replace("/", ".") for p in paths if p.endswith(".py")}
    return [m for m in floors if m in changed]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="hold mutmut's results to the floors in pyproject.toml")
    c.add_argument("--modules", nargs="*", default=[], help="only these guarded modules")
    sub.add_parser("select", help="changed paths on stdin -> modules= and globs= lines")
    args = parser.parse_args(argv)
    floors, accepted = load_config()
    if args.command == "select":
        modules = select(sys.stdin.read().splitlines(), floors)
        print(f"modules={' '.join(modules)}")
        print(f"globs={' '.join(m + '.*' for m in modules)}")
        return 0
    breaches, notes = check(floors, accepted, read_results(), args.modules)
    for line in notes:
        print(line)
    for line in breaches:
        print(f"FAIL {line}")
    print(f"mutation floor: {'FAILED' if breaches else 'ok'} ({len(breaches)} problems)")
    return 1 if breaches else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
