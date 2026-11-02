import warnings
from pathlib import Path

import pytest

from netcheck.cleanup import CleanupRegistry
from netcheck.timing import SlaWarning, Timing, WaitTimeout, evaluate

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
