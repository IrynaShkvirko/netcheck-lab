"""Where each test sample came from, and that every place reporting it tells the truth."""

from __future__ import annotations

from pathlib import Path

import pytest

from netcheck import provenance
from netcheck.provenance import ProvenanceError

from .intents import ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _folder(tmp_path: Path, provenance_text: str, files: list[str]) -> Path:
    for rel in files:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("{}\n")
    (tmp_path / provenance.FILE).write_text(provenance_text)
    return tmp_path


def test_every_sample_folder_says_how_each_sample_was_made() -> None:
    """Each folder of device samples has a provenance.yaml that lists exactly its files."""
    folders = sorted({p.parent for p in FIXTURES.rglob("*.json")} - {FIXTURES})
    roots = {f for f in folders if (f / provenance.FILE).exists()} | {
        f.parent for f in folders if (f.parent / provenance.FILE).exists()
    }
    assert roots, "no provenance.yaml under tests/unit/fixtures"
    for root in roots:
        provenance.load(root)  # raises with every mismatch
    orphans = [f for f in folders if f not in roots and f.parent not in roots]
    assert not orphans, f"sample folders without a provenance.yaml: {orphans}"


def test_load_lists_every_mismatch(tmp_path: Path) -> None:
    folder = _folder(
        tmp_path,
        "samples:\n"
        "  r1/a.json: {source: captured, nos: FRR 10.1, date: '2026-11-10'}\n"
        "  r1/gone.json: {source: documentation}\n"
        "  r1/b.json: {source: guessed}\n",
        ["r1/a.json", "r1/b.json", "r1/new.json"],
    )
    with pytest.raises(ProvenanceError) as exc:
        provenance.load(folder)
    msg = str(exc.value)
    assert "r1/b.json: source 'guessed' is not one of captured, documentation" in msg
    assert "r1/new.json: sample file not listed in provenance.yaml" in msg
    assert "r1/gone.json: listed in provenance.yaml but missing" in msg


def test_a_folder_without_provenance_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text("{}")
    with pytest.raises(ProvenanceError, match="provenance.yaml is missing"):
        provenance.load(tmp_path)


def test_mark_captured_turns_documentation_into_captured(tmp_path: Path) -> None:
    folder = _folder(
        tmp_path,
        "samples:\n"
        "  r1/a.json: {source: documentation, nos: docs, date: '2026-11-07'}\n"
        "  r1/b.json: {source: documentation, nos: docs, date: '2026-11-07'}\n",
        ["r1/a.json", "r1/b.json"],
    )
    assert provenance.status(folder) == provenance.Status(0, 2)
    provenance.mark_captured(folder, [folder / "r1" / "a.json"], "Cumulus Linux 5.9", "2026-11-12")
    samples = {s.path: s for s in provenance.load(folder)}
    assert samples["r1/a.json"] == provenance.Sample(
        "r1/a.json", "captured", "Cumulus Linux 5.9", "2026-11-12"
    )
    assert samples["r1/b.json"].source == "documentation"
    status = provenance.status(folder)
    assert (status.captured, status.total, status.verified) == (1, 2, False)
    assert status.line("NVUE") == "1 of 2 NVUE samples captured from a device"


def test_readme_states_the_nvue_sample_count_and_calls_it_unverified() -> None:
    """The README status must change when samples are captured, not stay 'done'."""
    status = provenance.status(FIXTURES / "nvue")
    readme = (ROOT / "README.md").read_text()
    assert status.line("NVUE") in readme
    assert ("**unverified**" in readme) == (not status.verified)
    if not status.verified:  # every mention of the NVIDIA device side says so
        at = [i for i in range(len(readme)) if readme.startswith("device side", i)]
        assert at, "the README no longer mentions the NVIDIA device side"
        for i in at:
            assert "unverified" in readme[i : i + 80], readme[i : i + 80]


def test_unit_run_header_and_summary_count_documentation_samples(pytester) -> None:  # type: ignore[no-untyped-def]
    """The unit suite says how many NVUE samples are real, in its header, and how many
    tests ran on documentation samples only, in its summary."""
    result = pytester.runpytest_subprocess(
        str(ROOT / "tests" / "unit" / "test_nvue.py"),
        "-p",
        "no:cacheprovider",
        f"--rootdir={ROOT}",
        "-k",
        "neighbors_are_per_link",
    )
    status = provenance.status(FIXTURES / "nvue")
    result.stdout.fnmatch_lines(
        [
            f"netcheck samples: {status.line('NVUE')}: the NVIDIA parsers are unverified",
            "netcheck: 1 tests ran on documentation samples only*",
        ]
    )


def test_lab_runs_on_the_nvidia_backend_say_its_parsers_are_unverified(pytester) -> None:  # type: ignore[no-untyped-def]
    pytester.makepyfile(test_x="def test_x():\n    pass\n")
    result = pytester.runpytest_inprocess("-p", "netcheck.pytest_plugin", "--backend", "nvidia")
    status = provenance.status(FIXTURES / "nvue")
    result.stdout.fnmatch_lines(
        [f"netcheck: NVIDIA parsers checked against {status.line('NVUE')}; the rest come from*"]
    )
    local = pytester.runpytest_inprocess("-p", "netcheck.pytest_plugin", "--backend", "local")
    assert "NVIDIA parsers" not in local.stdout.str()


def test_record_samples_marks_what_it_wrote_as_captured(tmp_path: Path) -> None:
    from netcheck.backends.nvidia_air import record_samples

    from .fakes_nvue import FakeNvueTransport

    root = tmp_path / "nvue"
    written = record_samples(FakeNvueTransport("r1"), root / "r1", provenance_root=root)
    assert written
    samples = provenance.load(root)
    assert {s.path for s in samples} == {f"r1/{f.name}" for f in written}
    assert {s.source for s in samples} == {"captured"}
    assert samples[0].nos == "Cumulus Linux (version unknown)"  # the fake serves no `system`
