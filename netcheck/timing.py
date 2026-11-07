"""Thresholds, polling and SLA handling (ADR-0004)."""

from __future__ import annotations

import os
import re
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml

from netcheck.transport.base import TransportError

T = TypeVar("T")

_EXPR = re.compile(r"^\s*([a-z_]+)\s*([+*])\s*([0-9.]+)\s*$")


class SlaWarning(UserWarning):
    """An SLA was breached while NETCHECK_SLA_MODE=record."""


class WaitTimeout(AssertionError):
    """`last` is the last value the probe returned, or the TransportError it raised."""

    def __init__(self, check: str, timeout: float, last: object) -> None:
        super().__init__(f"{check}: condition not met within {timeout:.1f}s; last value: {last!r}")
        self.check = check
        self.timeout = timeout
        self.last = last

    @property
    def error(self) -> TransportError | None:
        """The transport error of the last attempt, if it raised one."""
        return self.last if isinstance(self.last, TransportError) else None

    def describe(self, value: str) -> str:
        """`value` (what the last answer showed), or why there was no answer."""
        return f"no answer: {self.error}" if self.error is not None else value


def evaluate(value: float | int | str, variables: dict[str, float]) -> float:
    """Evaluate a threshold: a number, a variable name, or '<var> +|* <number>'."""
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip()
    if text in variables:
        return variables[text]
    m = _EXPR.match(text)
    if not m:
        raise ValueError(f"unsupported threshold expression {value!r}")
    name, op, num = m.group(1), m.group(2), float(m.group(3))
    if name not in variables:
        raise ValueError(f"unknown variable {name!r} in {value!r}")
    return variables[name] + num if op == "+" else variables[name] * num


@dataclass(frozen=True)
class Threshold:
    check: str
    timeout: float
    poll: float
    sla: float | None = None
    window: float | None = None
    stream_interval: float | None = None


@dataclass
class Measurement:
    check: str
    value_s: float
    sla_s: float | None
    breached: bool


@dataclass
class Timing:
    profile: str
    thresholds: dict[str, Threshold]
    factor: float = 1.0
    sla_mode: str = "strict"  # strict | record
    measurements: list[Measurement] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        path: Path,
        profile: str,
        variables: dict[str, float],
        env: dict[str, str] | None = None,
    ) -> Timing:
        env = dict(os.environ) if env is None else env
        factor = float(env.get("NETCHECK_TIME_FACTOR", "1"))
        sla_mode = env.get("NETCHECK_SLA_MODE", "strict")
        if sla_mode not in ("strict", "record"):
            raise ValueError(f"NETCHECK_SLA_MODE must be strict or record, got {sla_mode!r}")
        raw: dict[str, Any] = yaml.safe_load(path.read_text())["profiles"][profile]
        thresholds = {}
        for check, d in raw.items():
            thresholds[check] = Threshold(
                check=check,
                timeout=evaluate(d.get("timeout", 0), variables) * factor,
                poll=float(d.get("poll", 1)),
                sla=evaluate(d["sla"], variables) if "sla" in d else None,
                window=float(d["window"]) if "window" in d else None,
                stream_interval=float(d["stream_interval"]) if "stream_interval" in d else None,
            )
        return cls(profile=profile, thresholds=thresholds, factor=factor, sla_mode=sla_mode)

    def __getitem__(self, check: str) -> Threshold:
        return self.thresholds[check]

    def wait_until(
        self,
        check: str,
        probe: Callable[[], T],
        ok: Callable[[T], bool] = bool,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[T, float]:
        """Poll `probe` until `ok(value)`. Returns (value, elapsed). Never sleeps blindly.

        A TransportError from the probe (a device that did not answer this time) counts
        as "not yet" and is retried; if the time runs out it is the WaitTimeout's `last`,
        so the failure report can show it (ADR-0004). Other exceptions go straight through.
        """
        th = self.thresholds[check]
        start = clock()

        def attempt() -> tuple[T | None, TransportError | None]:
            try:
                return probe(), None
            except TransportError as exc:
                return None, exc

        value, error = attempt()
        while error is not None or not ok(value):  # type: ignore[arg-type]
            if clock() - start >= th.timeout:
                raise WaitTimeout(check, th.timeout, error if error is not None else value)
            sleep(th.poll)
            value, error = attempt()
        return value, clock() - start  # type: ignore[return-value]

    def observe(self, name: str, value_s: float) -> Measurement:
        """Record a measured value that has no SLA (for example a control-plane time
        reported next to the data-plane one). Unlike `record`, `name` needs no threshold."""
        m = Measurement(name, round(value_s, 3), None, False)
        self.measurements.append(m)
        return m

    def record(self, check: str, value_s: float) -> Measurement:
        """Record a measured value and enforce the SLA according to the mode."""
        sla = self.thresholds[check].sla
        breached = sla is not None and value_s > sla
        m = Measurement(check, round(value_s, 3), sla, breached)
        self.measurements.append(m)
        if breached:
            msg = f"{check}: measured {value_s:.3f}s exceeds SLA {sla:.3f}s"
            if self.sla_mode == "strict":
                raise AssertionError(msg)
            warnings.warn(msg, SlaWarning, stacklevel=2)
        return m
