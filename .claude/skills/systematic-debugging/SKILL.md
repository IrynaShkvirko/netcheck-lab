---
name: systematic-debugging
description: "Root-cause debugging for netcheck: failing lab tests, flaky convergence, restore failures, degraded labs, parser errors, CI lab failures. Use when a test fails, the lab misbehaves, or a fix attempt did not work."
---

# Systematic debugging (netcheck)

No fix without a root cause you can state as: "it fails because **X**, which causes **Y**
when **Z**." For a hard bug or a performance regression, also use
`mattpocock-skills:diagnosing-bugs`; this skill adds what is specific to netcheck.

## 1. Gather evidence

The framework already collected most of it. Read, in this order:

1. The failure report's **verdict** and **expected vs actual** (console, or
   `artifacts/<test>/report.txt`).
2. **Discriminating evidence** and **intent vs live config** in the same report.
3. **Device logs**: `artifacts/<test>/logs/<device>.jsonl`, every command and API call with
   raw response, rc/status and duration. The last calls before the failure usually explain it.
4. **Captures**: `artifacts/<test>/*.pcap` (summary already in the report).
5. **Run header**: backend, intent digest, active faults (`.netcheck/active-faults`), and
   the snapshot ID to reproduce with (`pytest --snapshot <id>` / `python -m netcheck restore <id>`).
6. **Recent changes**: `git log --oneline -10`, and whether the intent changed
   (`.netcheck/deployed-intent` vs the current digest).

Reproduce with one test: `uv run pytest tests/lab/test_x.py -k name --backend <b> -v`.
Note if it fails always, only after another test, or only on one backend.

## 2. Place the failure

| Symptom | First suspect | Check |
| --- | --- | --- |
| Fails only after another test | isolation: a restore or cleanup left state | run it alone; read the previous test's teardown in its log; `make lab-reset` |
| Session stops with exit code 3 | snapshot restore failed (`RestoreFailed`) | the listed devices' logs; was a link or config verified as still wrong? |
| "lab is running but degraded" / missing ports | container restarted and lost its veth links | `make lab-fresh` (never restore a degraded lab) |
| Passes locally, fails in CI | timing on shared runners | is it a timeout or an SLA? SLA in `record` mode is a warning; `NETCHECK_TIME_FACTOR` |
| Isolation "inconclusive" | the ping did not run (`PingResult.error`) | host log: rc >= 2, missing interface, missing route |
| OSPF neighbor not Full | timers, area, network type, MTU, link down | `interface_settings()` vs `Expected.ospf_interfaces`; `LinkState` both ends |
| Route missing, neighbors Full | passive interface or area of the originator | the route diagnosis in the report; `missing-ospf-network` fault still active? |
| `NvueShapeError` | NVUE schema differs from the samples | capture the response (`record_samples`), extend the parser, add the sample |
| NVUE revision stuck or failed | apply warnings, invalid config | `GET revision/<id>` body in the device log; `ays_yes` sent? |
| Air deploy times out | simulation state | the last Air state in the error; simulation history in Air |
| `TransportError` everywhere | access path down (docker, SSH jump, tunnel) | run one command by hand through the same path |

## 3. Hypothesis and test

- One hypothesis, one change at a time; confirm the failure before and its absence after.
- Prefer a unit test that reproduces it over the fakes (`ScriptedTransport`,
  `FakeNvueTransport`): it is fast, and it stays as the regression test.
- A hypothesis must explain every symptom, on every backend it was seen on.

## 4. Fix and verify

- Fix the cause in the layer that owns it: parser, adapter, lifecycle, timing value (with
  measurements), or the intent. Never edit a lab test to make a backend pass, and never add
  a sleep.
- Verify: the unit regression test, `make check`, then the lab test alone and the full
  suite (order effects).
- Commit message and PR say what broke, why, and what changed.

## Escalate after three failed attempts

Stop and re-check the model of the system: re-read the ADR of the area, the device log
end to end, and ask the user for what you cannot observe (Air console, a captured response,
the CI runner).
