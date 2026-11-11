"""CI configuration: which changes need the lab job, and the workflow rules that keep CI
reproducible (actions pinned by commit, locked dependencies, timeouts, triggers)."""

from __future__ import annotations

import re
import subprocess
import sys

import pytest
import yaml

from .intents import ROOT

SCRIPT = ROOT / "scripts" / "lab-affected.py"
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def lab_affected(paths: list[str]) -> str:
    out = subprocess.run(
        [sys.executable, str(SCRIPT)], input="\n".join(paths), capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.mark.parametrize(
    ("paths", "verdict"),
    [
        (["tests/unit/test_ci.py", "README.md"], "lab=false"),
        (["docs/decisions/0004-timing-and-thresholds.md", ".claude/agents/README.md"], "lab=false"),
        ([".github/dependabot.yml", ".gitignore"], "lab=false"),
        (["tests/unit/test_ci.py", "netcheck/render.py"], "lab=true"),
        (["tests/lab/test_vlan.py"], "lab=true"),
        (["timing.yaml"], "lab=true"),
        (["backends.yaml"], "lab=true"),
        ([".github/workflows/ci.yml"], "lab=true"),
        (["uv.lock"], "lab=true"),
        ([], "lab=true"),  # no list (manual run, first push): when in doubt, deploy
    ],
)
def test_which_changes_need_the_lab(paths: list[str], verdict: str) -> None:
    assert lab_affected(paths) == verdict


def _steps() -> list[tuple[str, dict]]:
    out = []
    for wf in WORKFLOWS:
        for job in yaml.safe_load(wf.read_text())["jobs"].values():
            out += [(wf.name, s) for s in job.get("steps", [])]
    return out


def test_every_action_is_pinned_to_a_commit() -> None:
    """A tag can be moved to other code; a commit SHA cannot. The tag stays as a comment
    so Dependabot and readers know the version."""
    loose = [
        f"{wf}: {s['uses']}"
        for wf, s in _steps()
        if "uses" in s and not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", s["uses"])
    ]
    assert not loose, f"actions not pinned by commit SHA: {loose}"
    for wf in WORKFLOWS:
        for line in wf.read_text().splitlines():
            if re.search(r"uses: \S+@[0-9a-f]{40}", line):
                assert re.search(r"# v\d+\.\d+\.\d+$", line), (
                    f"{wf.name}: no version comment: {line}"
                )


def test_dependencies_install_from_the_lock_file() -> None:
    syncs = [s["run"] for _, s in _steps() if "uv sync" in s.get("run", "")]
    assert syncs and all("--locked" in run for run in syncs), syncs


def test_every_job_has_a_timeout() -> None:
    for wf in WORKFLOWS:
        for name, job in yaml.safe_load(wf.read_text())["jobs"].items():
            assert "timeout-minutes" in job, f"{wf.name}: job {name} has no timeout-minutes"


def test_ci_runs_once_per_pr_commit_and_on_master() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    triggers = ci[True]  # YAML 1.1 reads the `on` key as True
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["master"]
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    matrix = ci["jobs"]["checks"]["strategy"]["matrix"]["python"]
    assert matrix == ["3.11", "3.12", "3.13", "3.14"]
    assert ci["jobs"]["lab"]["if"] == "needs.changes.outputs.lab == 'true'"


def test_every_lab_image_is_pinned_by_digest() -> None:
    """A tag such as :latest can point at another image tomorrow; a digest cannot."""
    images = yaml.safe_load((ROOT / "backends.yaml").read_text())["local"]["images"]
    loose = {
        role: ref for role, ref in images.items() if not re.search(r"@sha256:[0-9a-f]{64}$", ref)
    }
    assert not loose, f"images not pinned by digest (make pin-images): {loose}"
