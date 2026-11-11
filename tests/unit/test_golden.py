"""Golden render: what netcheck would deploy, as files a person has read.

model/render.py writes each router's FRR config from Expected, and the drift tests compare the
live lab with Expected, so the two agree by construction: a wrong assumption shared by
both passes every drift test. These files break that loop. They are the rendered lab for
every intent (topology, FRR configs, expected_state.json), checked in after review. Any
change to what would be deployed shows up here as a diff before it reaches a lab.

After an intended change: `make golden`, then read `git diff tests/unit/golden`.

The intent digest is replaced by a placeholder, so editing a comment in an intent file
does not touch the golden files; the image names come from backends.yaml, so pinning an
image does (it changes what is deployed).
"""

from __future__ import annotations

import difflib
import os
import shutil
from pathlib import Path

import pytest
import yaml

from netcheck.model.intent import load_intent
from netcheck.model.render import render

from .intents import INTENT_FILES, ROOT, intent_id

GOLDEN = Path(__file__).resolve().parent / "golden"
DIGEST = "<intent-digest>"
UPDATE = "NETCHECK_UPDATE_GOLDEN"
HINT = (
    "\nIf this change is intended: run `make golden`, then review "
    "`git diff tests/unit/golden` before committing it."
)


def rendered(intent_file: Path, out: Path) -> dict[str, str]:
    """Render the intent with the local backend's settings; {relative path: text}."""
    intent = load_intent(intent_file)
    settings = yaml.safe_load((ROOT / "backends.yaml").read_text())["local"]
    render(intent, settings["images"], out, settings.get("port_prefix", "eth"))
    return {
        p.relative_to(out).as_posix(): p.read_text().replace(intent.digest, DIGEST)
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }


def golden_files(folder: Path) -> dict[str, str]:
    if not folder.is_dir():
        return {}
    return {
        p.relative_to(folder).as_posix(): p.read_text()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def differences(golden: dict[str, str], live: dict[str, str]) -> list[str]:
    """One block per file that is missing, unexpected or different (a unified diff)."""
    out = [f"missing from the render: {f}" for f in sorted(set(golden) - set(live))]
    out += [f"not in the golden files: {f}" for f in sorted(set(live) - set(golden))]
    for f in sorted(set(golden) & set(live)):
        if golden[f] != live[f]:
            diff = difflib.unified_diff(
                golden[f].splitlines(),
                live[f].splitlines(),
                f"golden/{f}",
                f"rendered/{f}",
                lineterm="",
            )
            out.append("\n".join(diff))
    return out


@pytest.mark.parametrize("intent_file", INTENT_FILES, ids=intent_id)
def test_render_matches_the_reviewed_golden_files(intent_file: Path, tmp_path: Path) -> None:
    name = load_intent(intent_file).name
    live = rendered(intent_file, tmp_path)
    folder = GOLDEN / name

    if os.environ.get(UPDATE):
        shutil.rmtree(folder, ignore_errors=True)
        for rel, text in live.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text(text)
        pytest.skip(f"rewrote {folder.relative_to(ROOT)}: review the diff before committing")

    problems = differences(golden_files(folder), live)
    if problems:
        pytest.fail(
            f"the render of {intent_file.relative_to(ROOT)} differs from "
            f"tests/unit/golden/{name}:\n\n" + "\n\n".join(problems) + HINT,
            pytrace=False,
        )


def test_every_golden_folder_belongs_to_an_intent() -> None:
    """A golden folder left behind by a renamed or deleted intent would never be checked."""
    names = {load_intent(f).name for f in INTENT_FILES}
    orphans = sorted(p.name for p in GOLDEN.iterdir() if p.is_dir() and p.name not in names)
    assert not orphans, f"golden folders without an intent: {orphans} (delete them)"
