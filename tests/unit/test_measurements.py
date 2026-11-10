"""Measurements kept per lab session and over time, and the Markdown summary CI shows."""

from __future__ import annotations

import json
from pathlib import Path

from netcheck import measurements
from netcheck.timing import Measurement

CONTEXT = {"backend": "local", "intent": "campus-mini", "sla_mode": "strict", "repeats": 5}


def _run(time: str, outages: list[float], sla: float = 0.5, **extra: object) -> dict[str, object]:
    """A session as the failover test records it: each run observed, the median held to
    the SLA."""
    median = sorted(outages)[len(outages) // 2]
    measured = [
        ("t::test_failover", Measurement("reconverge_local_link_run", v, None, False))
        for v in outages
    ]
    measured.append(
        ("t::test_failover", Measurement("reconverge_local_link", median, sla, median > sla))
    )
    record = measurements.run_record(measured, {**CONTEXT, **extra})
    record["time"] = time
    return record


def test_save_writes_the_run_and_appends_the_history(tmp_path: Path) -> None:
    history = tmp_path / "h" / "measurements.jsonl"
    first, second = _run("t1", [0.06]), _run("t2", [0.08])
    measurements.save(first, tmp_path / "a1", history)
    measurements.save(second, tmp_path / "a2", history)
    assert json.loads((tmp_path / "a2" / "measurements.json").read_text()) == second
    assert measurements.load_history(history) == [first, second]
    assert measurements.load_history(tmp_path / "missing.jsonl") == []
    assert first["measurements"][-1] == {  # type: ignore[index]
        "test": "test_failover",
        "check": "reconverge_local_link",
        "value_s": 0.06,
        "sla_s": 0.5,
        "breached": False,
    }


def test_summary_compares_the_median_with_earlier_runs_of_the_same_lab() -> None:
    history = [
        _run("t1", [0.05, 0.06, 0.07]),  # median 0.06
        _run("t2", [0.06]),
        _run("t3", [9.0], intent="two-site"),  # another lab: not compared
    ]
    current = _run("t4", [0.08, 0.09, 0.12, 0.6, 0.07])  # median 0.09
    text = measurements.summary(current, history)
    assert "SLA mode strict, repeats 5" in text
    assert "| reconverge_local_link | 0.090 | 0.5 | 0.060 (2 runs) | +50% |" in text
    assert "| reconverge_local_link_run | 0.090 (median of 5) |  | 0.060 (2 runs) | +50% |" in text


def test_summary_marks_a_breach_and_a_first_run() -> None:
    text = measurements.summary(_run("t1", [0.7]), [])
    assert "| reconverge_local_link | 0.700 **SLA breach** | 0.5 | first run |  |" in text
    assert "| reconverge_local_link_run | 0.700 |  | first run |  |" in text


def test_a_lab_session_saves_its_measurements(pytester, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The plugin's session hook, given what its `lab` fixture records."""
    history = tmp_path / "history.jsonl"
    monkeypatch.setenv("NETCHECK_HISTORY", str(history))
    pytester.makeconftest(
        f"""
from pathlib import Path
from netcheck.pytest_plugin import MEASURED_KEY, SESSION_KEY
from netcheck.timing import Measurement

def pytest_configure(config):
    config.stash[SESSION_KEY] = {{"backend": "local", "intent": "campus-mini",
        "intent_digest": "abc", "sla_mode": "record", "repeats": 1,
        "artifacts": Path({str(tmp_path / "art")!r})}}
    m = Measurement("reconverge_local_link", 0.06, 2.0, False)
    config.stash[MEASURED_KEY] = [("t::test_failover", m)]
"""
    )
    pytester.makepyfile(test_x="def test_x():\n    pass\n")
    pytester.runpytest_inprocess("-p", "netcheck.pytest_plugin").assert_outcomes(passed=1)
    run = json.loads((tmp_path / "art" / "measurements.json").read_text())
    assert (run["backend"], run["sla_mode"], run["measurements"][0]["value_s"]) == (
        "local",
        "record",
        0.06,
    )
    assert "artifacts" not in run
    assert measurements.load_history(history) == [run]
