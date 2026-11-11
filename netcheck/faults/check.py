"""Prove the lab suite catches each fault: inject it, run the suite, hold it to the
tests the fault declares (`Fault.detected_by`), reset, and check those tests pass again.

`python -m netcheck fault-check` (`make fault-check`) runs this on the running lab; CI
runs it after the lab suite. Without it, nothing showed the suite can go red: a test that
always passes looks the same as a healthy network.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from netcheck.backends.base import Backend
from netcheck.faults.catalog import FAULTS, Detection

EXIT_LAB_UNTRUSTED = 3  # the pytest plugin's exit code when a restore failed


@dataclass(frozen=True)
class Outcome:
    """One test case from a JUnit report."""

    status: str  # passed | failed | error | skipped
    text: str = ""  # the failure or error report


def read_junit(path: Path) -> dict[str, Outcome]:
    """{test name with parameters: outcome} from a pytest JUnit XML file."""
    outcomes = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        status, text = "passed", ""
        for kind in ("failure", "error", "skipped"):
            node = case.find(kind)
            if node is not None:
                status = "failed" if kind == "failure" else kind
                text = (node.get("message") or "") + "\n" + (node.text or "")
                break
        outcomes[case.get("name", "")] = Outcome(status, text)
    return outcomes


def missed(detections: Sequence[Detection], outcomes: dict[str, Outcome]) -> list[str]:
    """Why each declared detection did not happen; empty when the suite caught the fault."""
    problems = []
    for d in detections:
        got = outcomes.get(d.test)
        if got is None:
            problems.append(f"{d.test}: did not run")
        elif got.status != "failed":
            problems.append(f"{d.test}: {got.status}, but must fail")
        else:
            problems += [
                f"{d.test}: failed, but its report does not say {s!r}"
                for s in d.shows
                if s not in got.text
            ]
    return problems


@dataclass
class FaultResult:
    name: str
    description: str = ""
    problems: list[str] = field(default_factory=list)
    also_failed: list[str] = field(default_factory=list)  # collateral, for information

    @property
    def ok(self) -> bool:
        return not self.problems


Run = Callable[[list[str]], int]


def run_subprocess(cmd: list[str]) -> int:
    return subprocess.run(cmd, check=False).returncode


def check_fault(
    backend: Backend, name: str, pytest_cmd: Sequence[str], artifacts: Path, run: Run
) -> FaultResult:
    """Inject `name` on a lab at its baseline, run the lab suite, reset, rerun the
    detecting tests. Leaves the lab at its baseline, also when something fails."""
    result = FaultResult(name)
    detections = FAULTS[name].detected_by(backend.intent)
    folder = artifacts / name
    folder.mkdir(parents=True, exist_ok=True)

    backend.reset()  # start from the baseline, whatever an earlier run left
    try:
        result.description = backend.inject_fault(name)
        junit = folder / "junit.xml"
        rc = run([*pytest_cmd, "tests/lab", f"--junitxml={junit}", f"--artifacts={folder}"])
    finally:
        backend.reset()

    if rc == EXIT_LAB_UNTRUSTED:
        result.problems.append("the lab could not be restored during the faulted run")
        return result
    if not junit.is_file():
        result.problems.append(f"the faulted run wrote no report (pytest exit code {rc})")
        return result
    outcomes = read_junit(junit)
    result.problems += missed(detections, outcomes)
    declared = {d.test for d in detections}
    result.also_failed = sorted(
        t for t, o in outcomes.items() if o.status in ("failed", "error") and t not in declared
    )

    after = folder / "after-reset.xml"
    node_ids = [d.node_id for d in detections]
    rc = run([*pytest_cmd, *node_ids, f"--junitxml={after}", f"--artifacts={folder / 'after'}"])
    if rc != 0:
        still = sorted(t for t, o in read_junit(after).items() if o.status != "passed")
        listing = ", ".join(still) if still else f"pytest exit code {rc}"
        result.problems.append(f"after the reset, the detecting tests do not pass: {listing}")
    return result


def check_faults(
    backend: Backend,
    pytest_cmd: Sequence[str],
    artifacts: Path,
    names: Sequence[str] | None = None,
    run: Run = run_subprocess,
    out: Callable[[str], None] = print,
) -> bool:
    """Check every fault (or `names`); print a summary; True if the suite caught all."""
    results = []
    for name in names or sorted(FAULTS):
        out(f"fault-check: {name}")
        r = check_fault(backend, name, pytest_cmd, artifacts, run)
        results.append(r)
    out("")
    out("fault-check summary")
    for r in results:
        out(f"  {r.name}: {'caught' if r.ok else 'NOT CAUGHT'} ({r.description or 'not injected'})")
        for d in FAULTS[r.name].detected_by(backend.intent):
            out(f"    must fail: {d.test}")
        for p in r.problems:
            out(f"    problem:   {p}")
        if r.also_failed:
            out(f"    also failed (fine, the fault breaks more): {', '.join(r.also_failed)}")
    return all(r.ok for r in results)
