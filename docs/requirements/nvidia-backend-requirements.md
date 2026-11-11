# Feature: NVIDIA backend (Cumulus Linux on NVIDIA DSX Air)

**Summary:** `--backend nvidia` deploys the intent as a DSX Air simulation of Cumulus Linux
switches and Ubuntu hosts, and the existing lab suite runs on it unchanged.

**Scope:** backend (lab lifecycle, transports, capabilities, snapshots), timing profile, CLI,
Makefile, CI (opt-in), docs. Tests and assertions are not changed.

**References:**
- ADR-0001 (two backends, one intent; Air open questions), ADR-0002 (composition, transports),
  ADR-0003 (lifecycle, snapshots; NVUE rollback open question), ADR-0004 (NVIDIA timing profile)
- Current code: `netcheck/backends/nvidia_air.py` (NVUE parsers and revision flow done, lab side
  stubbed), `netcheck/devices/transport/nvue_rest.py`, `tests/unit/test_nvue.py`, `tests/unit/fakes_nvue.py`
- Implementation plan: `docs/plans/2026-11-08-nvidia-backend-implementation-plan.md`
- DSX Air: [API authentication](https://docs.nvidia.com/dsx-air/authentication.md),
  [import a topology](https://docs.nvidia.com/dsx-air/api-reference/simulations/api-v-3-simulations-import-create.md),
  [start and stop](https://docs.nvidia.com/dsx-air/sdk/v1-11-0-examples-start-stop-simulations.md),
  [services](https://docs.nvidia.com/dsx-air/api-reference/services/api-v-3-simulations-nodes-interfaces-services-create.md),
  [OOB network](https://docs.nvidia.com/dsx-air/oob-management-network.md)
- NVUE: [REST API](https://docs.nvidia.com/networking-ethernet-software/cumulus-linux/System-Configuration/NVIDIA-User-Experience-NVUE/NVUE-API/)

Terms follow `GLOSSARY.md` (intent, expected state, logical port, capability, snapshot,
baseline, fault, degraded lab).

---

## 1. Topology from the intent

- REQ-1.1 The backend generates a DSX Air topology from the intent alone: every intent node becomes
  a simulation node, every intent link becomes a simulation link between real port names.
- REQ-1.2 Routers and switches run Cumulus Linux; hosts run a generic Ubuntu image. Image names are
  pinned in `backends.yaml` (`nvidia.images`), nowhere else.
- REQ-1.3 Logical ports map to `swp<N>` (`p1` -> `swp1`, `p1.10` -> `swp1.10`) through `PortMap`,
  as on every backend. Host ports use the host image's interface prefix (`nvidia.host_port_prefix`).
- REQ-1.4 The topology enables Air's out-of-band management network (`oob-mgmt-server`,
  `oob-mgmt-switch`); no test traffic uses it.
- REQ-1.5 `netcheck render --backend nvidia` writes the topology, the per-device NVUE configuration
  and `expected_state.json` to `build/<intent>/nvidia/` without calling Air.
- REQ-1.6 The rendered NVUE configuration of each router states exactly the OSPF interfaces
  `Expected.ospf_interfaces(router)` returns; the switch configuration states exactly
  `Expected.vlan_membership(switch)`. A unit test proves both against the existing NVUE parsers
  (the same round-trip rule the FRR side has).

## 2. Lab lifecycle

- REQ-2.1 `make lab-up BACKEND=nvidia` creates the simulation, starts it, waits until it is
  `ACTIVE` and every device answers, pushes the rendered configuration, and takes the baseline.
- REQ-2.2 The simulation ID is kept in `.netcheck/` so later commands (tests, restore, fault,
  destroy) reach the same simulation from another process.
- REQ-2.3 A running simulation built from the current intent and with every intended port is
  reused, exactly as `Backend.up()` decides for the local backend; `--fresh` replaces it.
- REQ-2.4 `NETCHECK_NO_DEPLOY` and `NETCHECK_RESET_ON_START` behave as on the local backend
  (they live in `Backend.up()`; the NVIDIA backend must not bypass it).
- REQ-2.5 `make lab-down BACKEND=nvidia` deletes the simulation and forgets its ID. Deleting a
  simulation that no longer exists succeeds.
- REQ-2.6 A simulation that fails to import, start or boot within the `device_ready` timeout stops
  the deploy with an error that names the Air state and the simulation ID.
- REQ-2.7 `is_running()` is true only when the recorded simulation exists and is `ACTIVE`; a
  missing token or an unreachable Air API is an error, not "not running".
- REQ-2.8 A degraded lab (an intended port missing on a device) is detected by the shared
  `missing_ports()` and redeployed, never restored.

## 3. Reaching the devices

- REQ-3.1 All device access goes through the simulation's `oob-mgmt-server`, exposed by one Air
  SSH service. No device is reachable from outside the simulation in any other way.
- REQ-3.2 NVUE REST calls reach each switch's API through an SSH tunnel; the API is not exposed on
  the management network.
- REQ-3.3 Host and switch shell commands run over SSH with the oob-mgmt-server as jump host.
- REQ-3.4 Every API call and command is recorded in the device log with the same fields as on the
  local backend; secrets are redacted (ADR-0005).
- REQ-3.5 Losing a tunnel or SSH session during a test is a `TransportError` (polling retries it,
  ADR-0004 rule 5), never a hang: every call has a timeout.

## 4. Capabilities

Every role gets the capabilities `ROLE_CAPABILITIES` allows, so no lab test is skipped on this
backend for a missing capability.

| Role | Links | Ospf | Bridge | Shell | Snapshot |
| --- | --- | --- | --- | --- | --- |
| router (Cumulus) | NVUE | NVUE | - | - | NVUE |
| switch (Cumulus) | NVUE | - | NVUE | SSH (Linux) | NVUE |
| host (Ubuntu) | SSH (Linux) | - | - | SSH (Linux) | SSH (links, host routes) |

- REQ-4.1 Host and switch shells behave as the local `Shell` contract states: a ping that did not
  run returns `PingResult` with `error`, `stop_capture` may be called twice, captures are copied
  to the test's artifact folder.
- REQ-4.2 Commands that need root on the Ubuntu and Cumulus images (sub-200 ms ping intervals,
  tcpdump, route changes) run with non-interactive privilege escalation; a missing privilege is a
  clear error.
- REQ-4.3 `Ospf.lsdb_prefixes()` returns the OSPF LSDB prefixes from NVUE (it is evidence only).
- REQ-4.4 Route parsing works on the Cumulus release pinned in `backends.yaml`; if that release
  uses the 5.11+ RIB shape, the parser supports it from a captured sample.
- REQ-4.5 The documentation-based NVUE samples in `tests/unit/fixtures/nvue/` are replaced by
  responses captured from the simulation (`record_samples`); the unit tests pass on them.

## 5. Snapshots and isolation (ADR-0003)

- REQ-5.1 A switch or router snapshot is its full applied NVUE configuration (links included).
- REQ-5.2 Restore applies that configuration as one NVUE revision that replaces the applied
  configuration, then re-reads it and raises if it still differs. Nothing is applied when it
  already matches.
- REQ-5.3 A host snapshot holds link admin state and host routes, restored and verified as on
  the local backend.
- REQ-5.4 Snapshot files keep format 2: backend name and intent digest are checked before any
  restore (`SnapshotMismatch`), every device is tried (`RestoreFailed`).
- REQ-5.5 The faults `wrong-vlan` and `missing-ospf-network` work through the capabilities and are
  removed by `make lab-reset BACKEND=nvidia`.

## 6. Timing (ADR-0004)

- REQ-6.1 The `nvidia` timing profile is measured, not guessed: boot time, interface up, OSPF Full,
  route installed, VLAN change and reconvergence are recorded on at least three runs and the
  profile is set from them with the method written in ADR-0004.
- REQ-6.2 SLAs run in `record` mode on Air until the measured spread allows `strict`.

## 7. Security and cost

- REQ-7.1 The Air API key (`NVIDIA_AIR_TOKEN`), the NVUE password (`NVUE_PASSWORD`) and SSH private
  keys come from the environment or the user's SSH agent only; they never appear in code, rendered
  files, device logs, reports, JUnit XML or CI artifacts.
- REQ-7.2 TLS to NVUE is verified unless `verify_tls: false` is set; over the SSH tunnel the
  certificate check may be relaxed only by that explicit setting, and the run header says so.
- REQ-7.3 SSH host keys of a simulation are trusted per simulation (a known-hosts file under
  `.netcheck/`), never by disabling host key checking globally.
- REQ-7.4 Every simulation the backend creates is named after the intent and the run, and has an
  expiry so a crashed run does not keep it alive.
- REQ-7.5 Values interpolated into shell commands or API paths come from the intent or the backend,
  are validated, and are quoted.

## 8. CLI, Makefile and CI

- REQ-8.1 `make lab-up | test-lab | lab-reset | lab-down | demo` accept `BACKEND=nvidia`.
- REQ-8.2 A missing `NVIDIA_AIR_TOKEN` or `NVUE_PASSWORD` stops `lab-up` before any Air call, with a
  message that names the variable.
- REQ-8.3 A separate CI workflow runs the lab suite on the NVIDIA backend on demand and nightly,
  only when the repository secret exists, and always deletes the simulation at the end.
- REQ-8.4 The default CI (`checks`, local `lab`) never needs Air or its secrets.

## 9. Documentation

- REQ-9.1 ADR-0001, ADR-0003 and ADR-0004 open questions are answered with what the work found;
  a new ADR records the access design (SSH jump and tunnels) and the snapshot design.
- REQ-9.2 README "Status" and the run instructions cover the NVIDIA backend; GLOSSARY gains any new
  domain term the work introduces.

## Out of scope

- MLAG, EVPN, BGP and other features the intent does not describe.
- Running tests from inside the oob-mgmt-server.
- Air Teleport, ZTP scripts beyond what first boot needs, checkpoints as snapshots.

---

## Acceptance criteria

### Lifecycle
- [ ] `make lab-up BACKEND=nvidia` from nothing ends with a baseline snapshot ID printed
- [ ] A second `make lab-up BACKEND=nvidia` reuses the simulation and keeps the baseline
- [ ] `make lab-down BACKEND=nvidia` leaves no simulation in the Air account

### Tests
- [ ] `make test-lab BACKEND=nvidia` passes with no test skipped for a missing capability
- [ ] `make demo BACKEND=nvidia` shows the wrong-vlan fault failing and the reset passing
- [ ] `make check` passes, with new unit tests for every new module (no Air needed)

### Isolation
- [ ] The failover test leaves every link up after its restore (verified by a later test)
- [ ] A forced restore failure stops the session with exit code 3

### Security
- [ ] `grep` over `artifacts/` and `build/` after a run finds neither secret
- [ ] A crashed run's simulation expires on its own

### Docs
- [ ] ADR open questions for the NVIDIA backend are closed or restated with what is still unknown
