# ADR-0005: Failure reports name the violated intent and carry the evidence

- Status: Accepted
- Date: 2026-11-02

## Context

"ping failed" is a symptom. A good network test failure tells the developer which
intent was violated, what differs, and where to look first. There is no
networking-specific report standard; we combine three references:

- **JUnit XML** as the machine format CI understands.
- **ISO/IEC/IEEE 29119-3** incident report content: summary, expected vs actual,
  environment, reproduction, evidence.
- **Network automation practice** (pyATS/Genie, Batfish, SuzieQ): diff intended
  state against operational state.

## Decision

Domain assertions raise `IntentViolation`, which carries a `FailureReport`. The pytest
hook completes it and renders it. Every report has eight parts:

1. **Verdict**: one line naming the violated intent, not the symptom.
2. **Expected vs actual**: a structured diff of state.
3. **Discriminating evidence**: the state that explains *why* (neighbor state next
   to routes, VLAN membership next to reachability).
4. **Intent vs live config diff**: usually points at the root cause.
5. **Commands and API calls run**, with raw JSON responses, in a per-device log.
6. **Packet capture**: a short text summary in the report, full `.pcap` as an artifact.
7. **Environment**: topology name, backend, NOS and image versions, intent hash.
8. **Reproduction**: the exact pytest command and the snapshot ID to restore.

### Where each part goes

| Part | Console | JUnit XML | Artifact |
| --- | --- | --- | --- |
| Verdict | first line | `<failure message>` | `report.json` |
| Diff, evidence, drift | yes | failure body | `report.txt` |
| Command log | no | `<system-out>` (failed tests only) | `logs/<device>.jsonl` |
| Capture | summary | failure body | `<device>-<port>.pcap` |
| Environment, measurements | yes | `<properties>` | `report.json` |
| Reproduction | yes | failure body | `report.txt` |

Artifacts are written to `artifacts/<test-id>/` and uploaded by CI.

## Consequences

- Assertions must collect evidence before raising; this is part of each assertion's
  contract and is unit-tested with fake devices.
- Device logs are attached for failed tests only, keeping passing runs readable.

## Revision (2026-11-03, code review)

- Evidence collection is guarded. If reading evidence fails (often because the device that
  broke is the one being asked), the report gets an `evidence unavailable` section and the
  verdict stays; before, the error replaced the whole report.
- A ping that never ran (container down, tool missing) is an error, not "0 replies". An
  isolation check with no probes sent is reported as inconclusive and fails.
- Device logs are bounded (5000 entries per device, responses cut at 8000 characters), and
  secret-looking fields in NVUE API bodies are redacted before they reach CI artifacts.

## Revision (2026-11-03, readable tests)

Lab tests are written as named steps (`netcheck.steps.step`). The steps of the failing test
are the first section of its report, the failed one marked, so the reader sees where the test
stopped before reading the evidence. Failures that are not intent violations (plain asserts,
errors) get the step list as an extra section. Steps are logged live with
`make test-lab-live`.

