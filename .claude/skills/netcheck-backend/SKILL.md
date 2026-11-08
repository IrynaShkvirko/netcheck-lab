---
name: netcheck-backend
description: "netcheck backend rules: capabilities, transports, parsers, lab lifecycle, snapshots, timing. Use when writing, reviewing or refactoring code in netcheck/backends/, netcheck/transport/, netcheck/assembly.py, netcheck/capabilities.py, or adding a NOS or backend (local Containerlab/FRR, NVIDIA DSX Air/NVUE)."
---

# netcheck backend rules

The architecture is fixed by the ADRs in `docs/decisions/`; this skill is their checklist
form. Vocabulary is `GLOSSARY.md`'s (intent, expected state, logical port, capability,
snapshot, baseline, fault, degraded lab). For good/bad code pairs see
[examples.md](examples.md).

Severity: **CRITICAL** breaks test results or the lab; **HIGH** breaks a cross-backend
contract or makes failures undiagnosable; **MEDIUM** hurts maintainability.

## The seam (ADR-0001, ADR-0002) — CRITICAL

- Tests and `netcheck/assertions/` see only capabilities and normalized types
  (`OspfNeighbor`, `Route`, `OspfInterface`, `PortVlans`, `LinkState`, `PingResult`). NOS
  syntax (`vtysh`, `nv`, `swp`, `eth`, NVUE paths) lives in a backend;
  `test_assertions_do_not_parse_nos_text` greps `netcheck/assertions/` for it.
- Logical ports in, logical ports out. Every adapter and parser translates through the
  device's `PortMap` (`real()`, `logical()`, `[]`); none builds `f"swp{n}"` itself.
- The intent stays NOS-free: no image, vendor, interface name or credential in
  `intent/lab.yaml`. Backend details go to `backends.yaml` (versions pinned there only).
- Every intent node is a `Device` via `assemble()`; a capability a backend cannot give is
  `None` from its `CapabilityFactory`, which makes tests skip with the backend's name.
  `ROLE_CAPABILITIES` is the one role table.
- A new NOS is a new factory + adapters + parsers. Tests, assertions, `Expected` and
  `ROLE_CAPABILITIES` do not change for it.

## Transports — HIGH

- Adapters depend on the protocols in `netcheck/transport/base.py`: `CommandTransport`
  (`run`, `copy_out`) or `ApiTransport` (`request`). Tests pass `ScriptedTransport` or
  `FakeNvueTransport`; a real transport is only built by a backend's `open_*` method.
- Every real transport records every call in the device log (`DeviceLog.record`, kind
  `exec` or `api`), with the response, rc/status and duration (ADR-0005). API bodies go
  through `redact()` first.
- Every call has a timeout. Unreachable or failed devices raise `TransportError`; callers
  that poll treat it as "not yet" (ADR-0004 rule 5).
- Shell commands are built from validated values and `shlex.quote`; never from free text.

## Parsers — HIGH

- Structured output only (`vtysh ... json`, `ip -j`, `bridge -j`, NVUE JSON). No regex over
  CLI text; the one exception is `parse_ping`, and FRR config text parsed in
  `parse_frr_interfaces` only.
- Strict: an unknown shape raises (`NvueShapeError`, `TransportError`) instead of returning
  empty state. Empty state reads as "the network is broken" and sends debugging the wrong way.
- A parser is a pure function of the response and a `PortMap`; adapters call it.
- Cross-backend contract: each backend's parser of OSPF interfaces / bridge membership
  returns exactly `Expected(intent)` on a healthy lab (round-trip tests in
  `test_expected.py` and `test_nvue.py`). A new parser gets the same test.

## Lab lifecycle (ADR-0003) — CRITICAL

- `Backend.up()` alone decides reuse vs redeploy and when a baseline is taken (only after a
  real deploy, never from a reused lab). A backend implements `deploy()` (build now),
  `destroy()`, `is_running()`, `devices()`, `environment()`, and does not bypass `up()`.
- `NETCHECK_NO_DEPLOY` and `NETCHECK_RESET_ON_START` work because they live in `up()`.
- `deploy()` resets cached devices (`self._devices = None`): adapters must not outlive the
  lab they were built for.
- A degraded lab (missing intended ports, `missing_ports()`) is redeployed, never restored.

## Snapshots (ADR-0003) — CRITICAL

- `Snapshot` is a device capability: `take()` returns JSON-able state only the adapter
  understands; `restore_to(state)` changes nothing when state already matches, applies the
  difference, **re-reads and raises if it still differs**. An unverified restore poisons
  every later test (seen in the lab mutation run).
- The shared part stays in `Backend`: file format 2, intent digest and backend name checked
  before any change (`SnapshotMismatch`), every device tried (`RestoreFailed`).
- A restore must never cut the framework off from the device (management interface,
  credentials). Guard it before applying.

## Timing (ADR-0004) — HIGH

- Wait by polling with `Timing.wait_until(check, probe, ok)` (`timing.yaml` profile per
  backend), never `time.sleep` for a fixed guess. Thresholds reference intent timers where they follow from them.
- Negative checks (isolation) run their full window; they never pass early.
- Measurements go through `Timing.record` (with SLA) or `observe` (without).

## Faults and failure reports — MEDIUM

- Faults (`netcheck/faults.py`) go through capabilities, break exactly one intent, and are
  recorded with `Backend.inject_fault` so the run header shows them.
- Error messages name the device, the logical port and what was expected vs found; they
  end up verbatim in failure reports (ADR-0005).

## Code style — MEDIUM

- Python 3.11+, `from __future__ import annotations`, full type hints
  (`disallow_untyped_defs`), `ruff check` and `ruff format` clean.
- Docstrings say what and why in plain sentences, with the ADR when a rule comes from one.
- Small pure functions for parsing and diffing; side effects at the edges (adapters,
  backend lifecycle).
- Match the file you are in: naming (`parse_<nos>_<thing>`, `<Nos><Capability>` classes),
  module docstring with STATUS when a part is unfinished.
