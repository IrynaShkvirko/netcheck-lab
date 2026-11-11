"""The mutation floor (scripts/mutation-floor.py): scores and survivors read from mutmut's
.meta files, held to the floors and accepted survivors in pyproject.toml; and which modules
a pull request must mutate."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from .intents import ROOT

SCRIPT = ROOT / "scripts" / "mutation-floor.py"
M = "netcheck.run.timing"


def run(cwd: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
    )


def lab(
    tmp_path: Path, codes: dict[str, int | None], floor: float, accepted: dict[str, str]
) -> Path:
    """A folder with a pyproject.toml and one mutmut .meta file holding `codes`."""
    lines = ["[tool.mutation-floor]", f'"{M}" = {floor}', "", "[tool.mutation-accepted]"]
    by_reason: dict[str, list[str]] = {}
    for name, why in accepted.items():
        by_reason.setdefault(why, []).append(name)
    lines += [f'"{why}" = {json.dumps(names)}' for why, names in by_reason.items()]
    (tmp_path / "pyproject.toml").write_text("\n".join(lines) + "\n")
    meta = tmp_path / "mutants" / "netcheck" / "run" / "timing.py.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"exit_code_by_key": {f"{M}.{k}": v for k, v in codes.items()}}))
    return tmp_path


# 8 killed (failed test, pytest error, timeout), 1 survivor, 1 no tests, 1 skipped -> 8/10
CODES: dict[str, int | None] = {f"x_a__mutmut_{i}": 1 for i in range(1, 7)}
CODES |= {"x_a__mutmut_7": 3, "x_a__mutmut_8": 36, "x_b__mutmut_1": 0, "x_c__mutmut_1": 33}
CODES |= {"x_d__mutmut_1": 34}
BOTH = {f"{M}.x_b__mutmut_1": "report wording", f"{M}.x_c__*": "lab-only path"}


def test_a_module_at_its_floor_with_every_survivor_accepted_passes(tmp_path: Path) -> None:
    out = run(lab(tmp_path, CODES, 80, BOTH), "check")
    assert out.returncode == 0, out.stdout
    assert f"{M}: 80.0% killed (8/10), floor 80%" in out.stdout
    assert "mutation floor: ok" in out.stdout


def test_a_score_below_the_floor_fails_even_with_every_survivor_accepted(
    tmp_path: Path,
) -> None:
    """Accepting a survivor never raises the score: both rules hold separately."""
    out = run(lab(tmp_path, CODES, 81, BOTH), "check")
    assert out.returncode == 1
    assert f"FAIL {M}: 80.0% killed (8/10), floor 81%: BELOW THE FLOOR" in out.stdout


@pytest.mark.parametrize("missing", [f"{M}.x_b__mutmut_1", f"{M}.x_c__*"])
def test_a_survivor_or_untested_mutant_without_a_reason_fails(tmp_path: Path, missing: str) -> None:
    accepted = {k: v for k, v in BOTH.items() if k != missing}
    out = run(lab(tmp_path, CODES, 80, accepted), "check")
    assert out.returncode == 1
    assert "survivor not accepted: " + missing.replace("*", "mutmut_1") in out.stdout


def test_an_accepted_entry_that_matches_no_survivor_fails(tmp_path: Path) -> None:
    """A killed mutant's old reason would excuse the next survivor of that name."""
    accepted = BOTH | {f"{M}.x_a__mutmut_1": "was a survivor once"}
    out = run(lab(tmp_path, CODES, 80, accepted), "check")
    assert out.returncode == 1
    assert f"matches no survivor (remove it): {M}.x_a__mutmut_1" in out.stdout


def test_a_partial_run_cannot_pass_for_a_full_one(tmp_path: Path) -> None:
    out = run(lab(tmp_path, CODES | {"x_e__mutmut_1": None}, 80, BOTH), "check")
    assert out.returncode == 1
    assert f"FAIL {M}: 1 of 11 mutants not checked" in out.stdout


def test_suspicious_and_segfault_results_count_against_the_score(tmp_path: Path) -> None:
    codes = {"x_a__mutmut_1": 1, "x_a__mutmut_2": 35, "x_a__mutmut_3": -11}
    accepted = {f"{M}.x_a__mutmut_[23]": "flaky in this test"}
    out = run(lab(tmp_path, codes, 30, accepted), "check")
    assert out.returncode == 0, out.stdout
    assert f"{M}: 33.3% killed (1/3)" in out.stdout


def test_a_score_well_above_the_floor_asks_to_raise_it(tmp_path: Path) -> None:
    out = run(lab(tmp_path, CODES, 70, BOTH), "check")
    assert out.returncode == 0
    assert "floor 70%: raise the floor to 80" in out.stdout


def test_a_reason_is_required_and_a_mutant_has_one(tmp_path: Path) -> None:
    out = run(lab(tmp_path, CODES, 80, BOTH | {f"{M}.x_a__mutmut_1": " "}), "check")
    assert out.returncode == 1
    assert "accepted without a reason" in out.stderr
    (tmp_path / "twice").mkdir()
    folder = lab(tmp_path / "twice", CODES, 80, BOTH)
    cfg = folder / "pyproject.toml"
    cfg.write_text(cfg.read_text() + f'"another reason" = ["{M}.x_b__mutmut_1"]\n')
    out = run(folder, "check")
    assert out.returncode == 1
    assert f"{M}.x_b__mutmut_1 is accepted twice" in out.stderr


def test_check_can_be_limited_to_the_modules_a_pr_mutated(tmp_path: Path) -> None:
    folder = lab(tmp_path, CODES, 80, BOTH)
    cfg = folder / "pyproject.toml"
    cfg.write_text(cfg.read_text().replace("-floor]", '-floor]\n"netcheck.model.intent" = 90'))
    assert run(folder, "check").returncode == 1  # netcheck.model.intent has no results
    assert run(folder, "check", "--modules", M).returncode == 0
    out = run(folder, "check", "--modules", "netcheck.model.ports")
    assert "not guarded modules: ['netcheck.model.ports']" in out.stdout


@pytest.mark.parametrize(
    ("paths", "modules"),
    [
        (["netcheck/run/timing.py", "netcheck/model/ports.py", "README.md"], [M]),
        (["netcheck/model/ports.py", "docs/x.md"], []),
        (["tests/unit/test_ports.py"], ["netcheck.model.intent", M]),
        (["pyproject.toml"], ["netcheck.model.intent", M]),
        (["conftest.py"], ["netcheck.model.intent", M]),
        (["tests/lab/test_vlan.py"], []),
        ([], ["netcheck.model.intent", M]),  # no list (manual run): every guarded module
    ],
)
def test_select_mutates_what_a_change_can_weaken(
    tmp_path: Path, paths: list[str], modules: list[str]
) -> None:
    folder = lab(tmp_path, CODES, 80, BOTH)
    cfg = folder / "pyproject.toml"
    cfg.write_text(cfg.read_text().replace("-floor]", '-floor]\n"netcheck.model.intent" = 90'))
    out = run(folder, "select", stdin="\n".join(paths))
    assert out.stdout.splitlines() == [
        f"modules={' '.join(modules)}",
        f"globs={' '.join(m + '.*' for m in modules)}",
    ]


def test_the_repo_floor_config_names_real_modules_with_reasons() -> None:
    tool = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]
    floors, accepted = tool["mutation-floor"], tool["mutation-accepted"]
    for module, floor in floors.items():
        path = ROOT / (module.replace(".", "/") + ".py")
        assert path.exists(), f"floor for a module that does not exist: {module}"
        assert 0 < floor <= 100
    for reason, patterns in accepted.items():
        assert len(reason.split()) >= 3, f"say why, not just '{reason}'"
        for pattern in patterns:
            assert any(pattern.startswith(m + ".") for m in floors), pattern
