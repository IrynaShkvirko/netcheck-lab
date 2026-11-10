"""The fault check: each fault's declared detectors, and the run that holds the lab to them."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from netcheck.faultcheck import Outcome, check_fault, check_faults, missed, read_junit
from netcheck.faults import FAULTS, Detection
from netcheck.intent import load_intent

from .intents import INTENT_FILES, ROOT, intent_id
from .test_review_fixes import _run_lab_suite


def write_junit(path: Path, cases: dict[str, tuple[str, str]]) -> None:
    """A pytest-style JUnit file: {test name: (status, report text)}."""
    body = []
    for name, (status, text) in cases.items():
        inner = ""
        if status == "failed":
            inner = (
                f'<failure message="{escape(text, {chr(34): "&quot;"})}">{escape(text)}</failure>'
            )
        elif status == "error":
            inner = f'<error message="setup failed">{escape(text)}</error>'
        elif status == "skipped":
            inner = '<skipped message="no capability" />'
        body.append(f'<testcase classname="t" name="{escape(name)}">{inner}</testcase>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(body)
        + "</testsuite></testsuites>"
    )


# ---- reading results --------------------------------------------------------------


def test_read_junit_tells_the_outcomes_apart(tmp_path: Path) -> None:
    junit = tmp_path / "j.xml"
    write_junit(
        junit,
        {
            "test_a[h1-h3]": ("failed", "INTENT VIOLATED: h1 and h3 must be isolated"),
            "test_b": ("passed", ""),
            "test_c": ("error", "boom"),
            "test_d": ("skipped", ""),
        },
    )
    got = read_junit(junit)
    assert {k: o.status for k, o in got.items()} == {
        "test_a[h1-h3]": "failed",
        "test_b": "passed",
        "test_c": "error",
        "test_d": "skipped",
    }
    assert "must be isolated" in got["test_a[h1-h3]"].text


def test_missed_names_every_way_a_detection_can_fail() -> None:
    d = [
        Detection("f.py", "caught", ("verdict",)),
        Detection("f.py", "wrong_reason", ("verdict", "drift line")),
        Detection("f.py", "passed", ()),
        Detection("f.py", "absent", ()),
        Detection("f.py", "errored", ()),
    ]
    outcomes = {
        "caught": Outcome("failed", "... verdict ..."),
        "wrong_reason": Outcome("failed", "verdict, but another cause"),
        "passed": Outcome("passed"),
        "errored": Outcome("error", "setup failed"),
    }
    assert missed(d, outcomes) == [
        "wrong_reason: failed, but its report does not say 'drift line'",
        "passed: passed, but must fail",
        "absent: did not run",
        "errored: error, but must fail",
    ]


# ---- the declarations hold on a fake lab --------------------------------------------


@pytest.mark.parametrize("intent_file", INTENT_FILES, ids=intent_id)
@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_each_fault_is_caught_by_the_tests_it_declares(  # type: ignore[no-untyped-def]
    pytester, fault: str, intent_file: Path
) -> None:
    """The real tests/lab files on a healthy fake lab with the fault injected through
    netcheck.faults: every declared test fails, and its report shows the declared text.
    Proves the declarations right without a lab; `make fault-check` proves the lab."""
    junit = pytester.path / "junit.xml"
    _run_lab_suite(
        pytester,
        False,
        f"--junitxml={junit}",
        intent=str(intent_file.relative_to(ROOT)),
        faults=(fault,),
    )
    detections = FAULTS[fault].detected_by(load_intent(intent_file))
    assert detections, f"{fault} declares no detecting test"
    assert missed(detections, read_junit(junit)) == []


def test_the_suite_passes_without_a_fault(pytester) -> None:  # type: ignore[no-untyped-def]
    """The control: on the healthy fake lab no declared detector fails."""
    junit = pytester.path / "junit.xml"
    _run_lab_suite(pytester, False, f"--junitxml={junit}")
    outcomes = read_junit(junit)
    intent = load_intent(ROOT / "intent" / "lab.yaml")
    for fault in FAULTS.values():
        for d in fault.detected_by(intent):
            assert outcomes[d.test].status == "passed", d.test


# ---- the run against a lab ----------------------------------------------------------


class ScriptedPytest:
    """Stands in for the pytest subprocesses: answers like a lab that does (or does not)
    catch the fault, and records what the lab looked like at each run."""

    def __init__(  # type: ignore[no-untyped-def]
        self, lab, catches: bool = True, rc: int | None = None, reset_heals: bool = True
    ) -> None:
        self.lab, self.catches, self.rc, self.reset_heals = lab, catches, rc, reset_heals
        self.calls: list[tuple[list[str], list[str]]] = []  # (command, faults applied)

    def __call__(self, cmd: list[str]) -> int:
        faults = self.lab.backend.state.active_faults()
        self.calls.append((cmd, faults))
        junit = Path(next(a for a in cmd if a.startswith("--junitxml=")).split("=", 1)[1])
        name = faults[0].split(":")[0] if faults else ""
        detections = FAULTS[name].detected_by(self.lab.intent) if name else []
        if "tests/lab" in cmd:  # the faulted run
            fail = self.catches and bool(detections)
            status = "failed" if fail else "passed"
            cases = {d.test: (status, "\n".join(d.shows) if fail else "") for d in detections}
            cases["test_unrelated"] = ("failed", "collateral") if fail else ("passed", "")
            write_junit(junit, cases)
            return self.rc if self.rc is not None else (1 if fail else 0)
        # the rerun of the detecting tests on the reset lab
        status = ("passed", "") if self.reset_heals else ("failed", "still broken")
        write_junit(junit, {c.split("::")[1]: status for c in cmd if "::" in c})
        return 0 if self.reset_heals else 1


@pytest.fixture
def lab_up(fake):  # type: ignore[no-untyped-def]
    lab, parts = fake
    lab.backend.up()  # deploys the fake lab and takes the baseline
    return lab, parts


def test_check_fault_injects_runs_resets_and_reruns(lab_up, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    lab, parts = lab_up
    run = ScriptedPytest(lab)
    result = check_fault(lab.backend, "wrong-vlan", ["pytest"], tmp_path, run)
    assert result.ok, result.problems
    assert result.description == "sw1:p3 (h3) moved from VLAN 20 to VLAN 10"
    assert result.also_failed == ["test_unrelated"]
    (faulted, active), (rerun, after) = run.calls
    assert "tests/lab" in faulted and active and active[0].startswith("wrong-vlan")
    assert not after, "the detecting tests must rerun on the reset lab"
    assert "tests/lab/test_vlan.py::test_hosts_in_different_vlans_are_isolated[h1-h3]" in rerun
    assert parts["bridge"].ports["p3"].vlans == (20,), "the lab is left at its baseline"


def test_a_fault_the_suite_misses_is_reported(lab_up, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    lab, parts = lab_up
    result = check_fault(
        lab.backend, "wrong-vlan", ["pytest"], tmp_path, ScriptedPytest(lab, False)
    )
    assert not result.ok
    assert result.problems == [
        "test_hosts_in_different_vlans_are_isolated[h1-h3]: passed, but must fail",
        "test_vlan_membership_matches_intent[sw1]: passed, but must fail",
    ]
    assert parts["bridge"].ports["p3"].vlans == (20,)
    assert not lab.backend.state.active_faults()


def test_detecting_tests_that_still_fail_after_the_reset_are_a_problem(  # type: ignore[no-untyped-def]
    lab_up, tmp_path: Path
) -> None:
    """The reset must bring the lab back: a detector still failing means it did not."""
    lab, _ = lab_up
    run = ScriptedPytest(lab, reset_heals=False)
    result = check_fault(lab.backend, "missing-ospf-network", ["pytest"], tmp_path, run)
    assert result.problems == [
        "after the reset, the detecting tests do not pass: "
        "test_ospf_route_is_learned[r1-10.0.30.0/24], test_ospf_routes_match_intent"
    ]


def test_a_failed_restore_during_the_run_is_a_problem(lab_up, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    lab, _ = lab_up
    result = check_fault(lab.backend, "wrong-vlan", ["pytest"], tmp_path, ScriptedPytest(lab, rc=3))
    assert result.problems == ["the lab could not be restored during the faulted run"]


def test_check_faults_summarizes_every_fault(lab_up, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    lab, _ = lab_up
    lines: list[str] = []
    ok = check_faults(lab.backend, ["pytest"], tmp_path, run=ScriptedPytest(lab), out=lines.append)
    assert ok
    summary = lines[lines.index("fault-check summary") :]
    assert (
        "  missing-ospf-network: caught (r2:p3 (eth3, 10.0.30.1/24) removed from OSPF area 0)"
        in summary
    )
    assert "    must fail: test_ospf_route_is_learned[r1-10.0.30.0/24]" in summary
    assert any(ln.startswith("  wrong-vlan: caught") for ln in summary)
