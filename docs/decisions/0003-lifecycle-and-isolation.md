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

## Consequences

- Registry first, snapshot second: the registry undoes test actions in reverse
  order, then the snapshot restore corrects anything still drifted.
- A cleanup failure is reported but does not hide the original test failure.
- `make lab-reset` restores the baseline snapshot, which also removes injected faults.

## Open questions

- NVUE: confirm via the API how to roll back to an earlier applied revision.
