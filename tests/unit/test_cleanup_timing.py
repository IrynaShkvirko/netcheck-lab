import warnings
from pathlib import Path

import pytest

from netcheck.cleanup import CleanupRegistry
from netcheck.timing import SlaWarning, Threshold, Timing, WaitTimeout, evaluate

ROOT = Path(__file__).resolve().parents[2]


def test_cleanup_runs_lifo_and_survives_errors() -> None:
    reg = CleanupRegistry()
    order: list[str] = []
    reg.register("first", lambda: order.append("first"))
    reg.register("broken", lambda: 1 / 0)
    reg.register("third", lambda: order.append("third"))
    errors = reg.run()
    assert order == ["third", "first"]
    assert [e.description for e in errors] == ["broken"]
    assert len(reg) == 0


def test_threshold_expressions_use_intent_timers() -> None:
    v = {"dead_interval": 4.0}
    assert evaluate("dead_interval + 2", v) == 6.0
    assert evaluate("dead_interval * 1.5", v) == 6.0
    assert evaluate(3, v) == 3.0
    with pytest.raises(ValueError, match="unsupported"):
        evaluate("__import__('os')", v)


def test_timing_profile_scales_timeouts_not_slas() -> None:
    t = Timing.load(
        ROOT / "timing.yaml",
        "local",
        {"dead_interval": 4, "hello_interval": 1},
        env={"NETCHECK_TIME_FACTOR": "2"},
    )
    th = t["reconverge_remote_no_bfd"]
    assert th.sla == 6.0
    assert th.timeout == 30.0


def test_wait_until_polls_without_blind_sleep() -> None:
    t = Timing.load(
        ROOT / "timing.yaml", "local", {"dead_interval": 4, "hello_interval": 1}, env={}
    )
    values = iter(["Init", "2-Way", "Full"])
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    slept: list[float] = []
    value, elapsed = t.wait_until(
        "ospf_neighbor_full",
        lambda: next(values),
        lambda s: s == "Full",
        sleep=slept.append,
        clock=lambda: next(clock),
    )
    assert value == "Full" and slept == [1.0, 1.0]


def test_wait_until_times_out_with_last_value() -> None:
    t = Timing.load(
        ROOT / "timing.yaml", "local", {"dead_interval": 4, "hello_interval": 1}, env={}
    )
    ticks = iter(range(100))
    with pytest.raises(WaitTimeout, match="Init"):
        t.wait_until(
            "ospf_neighbor_full",
            lambda: "Init",
            lambda s: s == "Full",
            sleep=lambda _: None,
            clock=lambda: float(next(ticks) * 10),
        )


def test_sla_strict_fails_record_warns() -> None:
    v = {"dead_interval": 4, "hello_interval": 1}
    strict = Timing.load(ROOT / "timing.yaml", "local", v, env={"NETCHECK_SLA_MODE": "strict"})
    with pytest.raises(AssertionError, match="exceeds SLA"):
        strict.record("reconverge_local_link", 3.5)
    record = Timing.load(ROOT / "timing.yaml", "local", v, env={"NETCHECK_SLA_MODE": "record"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        m = record.record("reconverge_local_link", 3.5)
    assert m.breached and any(issubclass(w.category, SlaWarning) for w in caught)


def test_a_value_exactly_at_the_sla_meets_it() -> None:
    v = {"dead_interval": 4, "hello_interval": 1}
    strict = Timing.load(ROOT / "timing.yaml", "local", v, env={"NETCHECK_SLA_MODE": "strict"})
    sla = strict.thresholds["reconverge_local_link"].sla
    assert sla is not None
    m = strict.record("reconverge_local_link", sla)
    assert (m.check, m.value_s, m.sla_s, m.breached) == ("reconverge_local_link", sla, sla, False)
    assert strict.measurements == [m]


def test_a_recorded_value_is_rounded_to_milliseconds_and_a_breach_warns() -> None:
    v = {"dead_interval": 4, "hello_interval": 1}
    record = Timing.load(ROOT / "timing.yaml", "local", v, env={"NETCHECK_SLA_MODE": "record"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        m = record.record("reconverge_local_link", 0.23456)
    assert (m.value_s, m.sla_s, m.breached) == (0.235, 0.2, True)
    [w] = caught
    assert str(w.message) == "reconverge_local_link: measured 0.235s exceeds SLA 0.200s"


def _profile(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "timing.yaml"
    path.write_text("profiles:\n  unit:\n" + body)
    return path


def test_load_reads_every_field_and_fills_the_defaults(tmp_path: Path) -> None:
    path = _profile(
        tmp_path,
        "    full: {timeout: dead_interval + 2, poll: 0.5, sla: 3, window: 2.5,"
        " stream_interval: 0.02}\n"
        "    bare: {}\n",
    )
    t = Timing.load(path, "unit", {"dead_interval": 4.0}, env={"NETCHECK_TIME_FACTOR": "3"})
    assert (t.profile, t.factor) == ("unit", 3.0)
    assert t["full"] == Threshold(
        "full", timeout=18.0, poll=0.5, sla=3.0, window=2.5, stream_interval=0.02
    )
    assert t["bare"] == Threshold("bare", timeout=0.0, poll=1.0)  # no wait, poll each second


def test_load_rejects_an_unknown_sla_mode_or_timer(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="NETCHECK_SLA_MODE must be strict or record, got 'soft'"):
        Timing.load(ROOT / "timing.yaml", "local", {}, env={"NETCHECK_SLA_MODE": "soft"})
    path = _profile(tmp_path, "    x: {timeout: hold_time + 1}\n")
    with pytest.raises(ValueError, match="unknown variable 'hold_time' in 'hold_time \\+ 1'"):
        Timing.load(path, "unit", {"dead_interval": 4.0}, env={})


def test_wait_until_gives_up_at_the_timeout_and_reports_the_time_it_took() -> None:
    """A condition met on the third poll took 3 s from the first; one still unmet when
    exactly `timeout` has passed has timed out."""
    t = Timing("unit", {"x": Threshold("x", timeout=2.0, poll=1.0)})
    clock = iter([100.0, 101.0, 102.0, 103.0])
    with pytest.raises(WaitTimeout) as exc:
        t.wait_until(
            "x",
            lambda: "Init",
            lambda s: s == "Full",
            sleep=lambda _: None,
            clock=lambda: next(clock),
        )
    assert (exc.value.check, exc.value.timeout, exc.value.last) == ("x", 2.0, "Init")
    assert next(clock) == 103.0  # it stopped at 102, exactly 2 s after it started
    t = Timing("unit", {"x": Threshold("x", timeout=10.0, poll=1.0)})
    values, clock = iter(["Init", "Init", "Full"]), iter([100.0, 101.0, 102.0, 103.0])
    _, elapsed = t.wait_until(
        "x",
        lambda: next(values),
        lambda s: s == "Full",
        sleep=lambda _: None,
        clock=lambda: next(clock),
    )
    assert elapsed == 3.0
