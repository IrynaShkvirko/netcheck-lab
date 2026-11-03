# ADR-0003: Deploy once per session, isolate tests with snapshots and a cleanup registry

- Status: Accepted (supersedes the initial "deploy per test" idea)
- Date: 2026-11-02

## Context

The demo budget is 3-5 minutes. A container lab takes tens of seconds to deploy and
a VM-based NOS takes minutes, so redeploying per test does not fit. Tests still must
not leak state: a failover test leaves a link down, a debug test adds host routes.

## Options

1. Deploy per test. Perfect isolation, far too slow.
2. Deploy per session, no isolation. Fast, order-dependent and flaky.
3. Deploy per session plus two isolation layers.

## Decision

Option 3.

- **Session:** the lab is deployed once and reused if already running
  (`make lab-up` before a demo). `--fresh-lab` forces a redeploy. A baseline
  snapshot is taken right after deploy.
- **Layer 1, snapshot and rollback per test:** before each test the backend takes a
  snapshot of device configuration and link state; after the test it restores it.
  - Local: FRR running config (restored by a section diff applied with `vtysh`), bridge VLAN
    membership, link admin state, stored as JSON in `.netcheck/snapshots/`.
  - NVIDIA: NVUE configuration revisions.
- **Layer 2, cleanup registry:** every test-created resource registers its undo
  action at creation time. Undo actions run LIFO in a fixture finalizer, also when
  the test fails. Covers what a config snapshot cannot: host routes, `tcpdump`
  processes, neighbor cache changes, temp files.

**Resolved open question: what "snapshot" means on the local backend.** It is a
JSON document of FRR config text, VLAN membership and link state, with an ID. The ID
is printed in every failure report so the exact state can be restored
(`python -m netcheck restore <id>`).

### Revision (2026-11-02, first lab run)

The first design restored FRR with `frr-reload.py`. On the first real run it failed in
every test: the script is not usable inside the FRR container image and reported
nothing on stderr. Replaced with `frr_config_diff`: compare snapshot and live config per
section, apply only the missing lines (and `no <line>` for extra ones) through `vtysh`,
then re-read and verify. No restore runs when nothing changed, which is the common case.

### Revision (2026-11-03, code review)

- **Baseline policy.** A baseline is taken only right after a real deploy (or when none
  exists), never from a reused lab, which may still carry an injected fault.
  `Backend.up()` holds this rule for the CLI and the pytest session alike.
- **Active faults and deployed intent.** `.netcheck/active-faults` lists faults injected
  since the last baseline restore; every pytest run prints them in its header.
  `.netcheck/deployed-intent` records the intent digest the lab was built from; a running
  lab built from another intent is redeployed.
- **Teardown failures are visible.** A failed snapshot restore stops the session
  (exit code 3), because later results would be blamed on the wrong tests. A failed undo
  step with a successful restore is reported as an error at teardown, not as a warning.
- **Host routes** are part of snapshots and restored by diff (connected routes ignored).
- **Per-test artifacts** are cleared when the test starts, so old reports never sit next
  to new ones.
- **Serial only.** Lab tests refuse pytest-xdist workers: all workers would drive the same
  containers.
- **Snapshots are pruned** to the newest 50, never deleting the baseline.

## Consequences

- Registry first, snapshot second: the registry undoes test actions in reverse
  order, then the snapshot restore corrects anything still drifted.
- A cleanup failure is reported but does not hide the original test failure.
- `make lab-reset` restores the baseline snapshot, which also removes injected faults.

## Open questions

- NVUE: confirm via the API how to roll back to an earlier applied revision.
