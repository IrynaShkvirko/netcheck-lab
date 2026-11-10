# netcheck-lab

An intent-driven network test framework. One YAML file describes what the network
must do; the framework builds the lab from it, tests the live network against it,
and explains every failure in terms of the intent that was violated.

It runs on two backends behind one interface:

- **local**: FRR routers, a Linux-bridge switch and Linux hosts in Containerlab. Runs on a laptop and in GitHub Actions.
- **nvidia**: Cumulus Linux on NVIDIA DSX Air over the NVUE REST API. The device side is written
  but unverified (tested only on samples written from documentation);
  the lab side (creating the simulation, access, snapshots) is specified and planned, not built
  yet (see Status).

## Topology

```
 h1 (10.0.10.11) --p1\                                   primary, cost 10
 h2 (10.0.10.12) --p2- sw1 -p4 == trunk 10,20 == r1 ======================== r2 --p3-- h4 (10.0.30.14)
 h3 (10.0.20.13) --p3/          VLAN 10 gw 10.0.10.1  ======================== /
                                                         backup, cost 100
 VLAN 10 "users" is routed by r1.  VLAN 20 "isolated" has no gateway.
 OSPF area 0, point-to-point, hello 1 s / dead 4 s.
```

Source: [`intent/lab.yaml`](intent/lab.yaml). Ports are logical (`p1`); the local backend maps them to `eth1`, Cumulus to `swp1`.
Every link between two routers sets `ospf_cost`: the costs decide the path, and the route
tests check that each router uses the cheapest one (every one of them when costs tie).

## What the tests prove

| Marker | Tests | Intent |
| --- | --- | --- |
| `smoke` | ports up, OSPF running | the lab is usable |
| `functional` | VLAN isolation (at layer 2 and through a router; silence counts only after the sender's ARP probes are seen to leave it and it reaches its own VLAN), reachability, OSPF adjacency, learned routes over the cheapest path | every pair and route listed in the intent |
| `resilience` | primary link failure | traffic reroutes via backup; outage measured with a 10 ms ping stream (RFC 6413 method) |
| `drift` | VLAN membership, route table | live state equals the expected state generated from the intent |

All cases are parametrized from the intent: add a VLAN or a host and the tests follow.

## Running it

### 1. Prerequisites

| Tool | Why | Check |
| --- | --- | --- |
| Linux host (or a Linux VM) | Containerlab only runs on Linux | `uname -s` prints `Linux` |
| Docker | runs the lab nodes | `docker ps` works without sudo |
| Containerlab 0.76 | wires the topology | `containerlab version` |
| uv | Python env and dependencies | `uv --version` |
| make | task runner | `make --version` |

**On macOS** (Intel or Apple Silicon), run everything inside a Linux VM. OrbStack is the
simplest; it mounts your Mac folders at the same path inside the VM:

```bash
brew install orbstack
orb create ubuntu:24.04 netlab
orb -m netlab                     # shell inside the VM; run all following steps here
```

### 2. One-time setup (inside Linux)

```bash
curl -sL https://containerlab.dev/setup | sudo -E bash -s "all"   # Docker (+ latest Containerlab)
sudo usermod -aG docker $USER && newgrp docker                     # docker without sudo
sudo apt-get install -y make
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc

cd /path/to/netcheck-lab
make install-clab     # Containerlab at the version pinned in backends.yaml, checksum-verified
make install          # Python dependencies
make check            # lint + types + unit tests; no lab needed
```

`make install-clab` (`scripts/install-clab.sh`) downloads the release `.deb` with retries,
checks it against the release's `checksums.txt` and installs it with `dpkg` (Debian/Ubuntu).
CI installs Containerlab the same way.

`make check` should end with all unit tests passing. If it does not, fix that first: the
problem is the Python setup, not the lab.

### 3. Start the backend (the lab)

```bash
make lab-up
```

This renders the topology and configs from `intent/lab.yaml`, deploys the lab (first run
pulls images, a few minutes), and saves a **baseline snapshot**. If the lab is already
running and healthy it is reused; if it is running but degraded, or was built from a different
version of the intent, it is redeployed.

The baseline is taken only right after a real deploy, never from a reused lab, so a fault left
over from a demo can never become the state `make lab-reset` restores. After a reuse, `make lab-up`
prints `kept baseline <id>` and warns if faults are still applied.

Check it by hand before running tests:

```bash
sudo containerlab inspect -t build/campus-mini/campus-mini.clab.yml   # 7 nodes running
docker exec clab-campus-mini-r1 vtysh -c "show ip ospf neighbor"      # 2 neighbors, Full
docker exec clab-campus-mini-h1 ping -c3 10.0.30.14                   # h1 -> h4 works
docker exec clab-campus-mini-h1 ping -c3 -W1 10.0.20.13               # h1 -> h3 fails (isolated)
```

OSPF needs a few seconds after deploy to reach Full; repeat the neighbor check if it is empty.

Lab commands:

| Command | What it does |
| --- | --- |
| `make lab-up` | deploy (or reuse) and take the baseline snapshot |
| `make lab-reset` | restore the baseline snapshot; removes injected faults |
| `make lab-fresh` | destroy and redeploy from scratch |
| `make lab-down` | destroy the lab |
| `make fault-wrong-vlan` | demo fault: move h3's switch port into VLAN 10 |
| `make fault-missing-ospf` | demo fault: remove r2's LAN from OSPF |
| `make fault-check` | inject each fault in turn and prove the tests it declares catch it, then reset (also run in CI) |

### 4. Run the tests

```bash
make smoke            # lab is usable (2 tests, seconds)
make test-lab         # full suite (16 tests)
make demo             # interview flow, see below
```

Run a subset with pytest directly:

```bash
uv run pytest tests/lab -m functional                       # by marker: smoke, functional, resilience, drift
uv run pytest tests/lab -k "isolated and h1-h3"             # one parametrized case
uv run pytest tests/lab --fresh-lab                         # redeploy before the session
uv run pytest tests/lab --snapshot <id>                     # reproduce: restore a saved state first
NETCHECK_SLA_MODE=record uv run pytest tests/lab            # record SLA breaches instead of failing
NETCHECK_TIME_FACTOR=2 uv run pytest tests/lab              # double every timeout on a slow machine
```

Each lab test is written as named steps, and `make test-lab-live` prints them as they run:

```
tests/lab/test_ospf.py::test_ospf_route_is_learned[r1-10.0.30.0/24]
  step 1: Work out from the intent's OSPF costs which way r1 reaches 10.0.30.0/24
  step 2: Check r1 has a Full OSPF adjacency with r2 via p2
  step 3: Wait until 10.0.30.0/24 is installed in r1's routing table
  step 4: Wait until the route goes via r2 (10.0.12.1), the cheapest path
PASSED
```

When a test fails, its report starts with the same list, the failed step marked `FAILED`.

Every test starts from a fresh snapshot and is rolled back afterwards, so tests can run in
any order and a failing test does not leave the lab broken. Snapshots cover link state, router
configs, switch VLANs and host routes.

If the rollback after a test fails, the run stops (exit code 3) instead of letting later tests
run on a lab in an unknown state. If only an undo step failed but the rollback worked, the test
is reported as `ERROR at teardown`. Lab tests always run serially: one lab is shared, so
`pytest -n` (pytest-xdist) is refused.

When faults are applied, every pytest run says so in its header
(`netcheck: FAULTS ARE APPLIED to the lab`), until `make lab-reset`.

### 5. Read the results

| Output | Where |
| --- | --- |
| Pass/fail and failure reports | the console |
| Measured timings (convergence, adjacency) | `netcheck measurements` section at the end of the run |
| JUnit XML for CI | `artifacts/junit.xml` (measurements as `<properties>`) |
| Per failed test | `artifacts/<test-id>/report.txt`, `report.json`, `logs/<device>.jsonl`, `*.pcap` |
| Cleanup problems | `artifacts/<test-id>/cleanup-errors.txt` |
| Snapshots | `.netcheck/snapshots/<id>.json` |

Open a capture with `tcpdump -nn -r artifacts/<test-id>/h3-p1.pcap` or Wireshark.

## Demo (3-5 minutes)

```bash
make demo
```

1. Smoke tests pass.
2. `make fault-wrong-vlan` moves h3's switch port into VLAN 10.
3. The isolation test fails. This is intended and make continues; if the test passed
   instead, the demo stops with an error, because the suite would have missed the fault.
   The report, from a real run:

```
INTENT VIOLATED: h1 (VLAN 10) and h3 (VLAN 20) must be isolated

Expected: 0/20 ICMP replies h1 -> h3 over L2
Actual:   20/20 replies

Intent vs live:
  sw1:p1: expected 'access VLAN 10', actual 'access VLAN 10'
  sw1:p3: expected 'access VLAN 20', actual 'access VLAN 10'   <-- drift

Evidence:
  sw1 VLAN membership:
    VLAN 10: p1, p2, p3, p4
    VLAN 20: p4
  h1 neighbor table:
    10.0.20.13 lladdr aa:c1:ab:18:14:1d REACHABLE

Capture (summary):
  h3:p1
  15:05:09.821681 ARP, Request who-has 10.0.20.13 tell 10.0.10.11, length 28
  15:05:09.821722 ARP, Reply 10.0.20.13 is-at aa:c1:ab:18:14:1d, length 28
  15:05:09.821782 IP 10.0.10.11 > 10.0.20.13: ICMP echo request, id 1042, seq 1, length 64
  15:05:09.822081 IP 10.0.20.13 > 10.0.10.11: ICMP echo reply, id 1042, seq 1, length 64
  ...
  file: artifacts/<test-id>/h3-p1.pcap

Env: topology=campus-mini  backend=local  intent=61de9bdddff0  containerlab=0.76.0
     image.router=quay.io/frrouting/frr:10.1.0  nos=FRRouting 10.1_git (r1)
Repro: pytest 'tests/lab/test_vlan.py::test_hosts_in_different_vlans_are_isolated[h1-h3]' --backend local --snapshot <id>
Restore state: python -m netcheck restore <id>
```

   How to read it: the verdict names the intent; the drift line points at the root cause
   (`sw1:p3` in the wrong VLAN); the capture proves h1 and h3 now share a broadcast domain.

4. `make lab-reset` restores the baseline, then the failover test takes the primary link down
   and prints the measured outage in the `netcheck measurements` section.

The second fault, `make fault-missing-ospf`, removes r2's LAN from OSPF. Adjacencies stay
Full but the route disappears, and the report says so: "r1: adjacency Full, but r2 is not
advertising 10.0.30.0/24", with the interface drift (`r2:p3 (eth3): expected OSPF area 0,
actual not in OSPF`).
Run it with `make fault-missing-ospf && uv run pytest tests/lab -m functional`, then `make lab-reset`.

## Fault check

A suite that never fails proves nothing: a test that always passes looks the same as a
healthy network. Every fault in `netcheck/faults.py` therefore declares the lab tests that
must catch it, and the text each failure report must show (for `wrong-vlan`: the isolation
test for the pair, naming the drifted port, and the VLAN drift test).

```bash
make lab-up
make fault-check            # or: uv run python -m netcheck fault-check wrong-vlan
```

For each fault it restores the baseline, injects the fault, runs the lab suite, checks every
declared test failed with its text, restores the baseline again and reruns those tests,
which must pass. It prints a summary and exits 1 if any fault went unnoticed. Reports go to
`artifacts/fault-check/<fault>/`. CI runs it after the lab suite. The unit tests hold the
declarations to the fake lab for every intent (`tests/unit/test_faultcheck.py`), so a new
fault without a detecting test fails `make check`.

## Troubleshooting

Start with the error text: command failures name the container they ran on
(`clab-campus-mini-r1: 'ip -j link show eth1' failed ...`).

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Device "eth1" does not exist`, or `lab links are gone (r1:p1 (eth1))` | A container restarted (Docker restart, VM or laptop sleep) and lost its links; Containerlab wires links only at deploy | `make lab-fresh` (`make lab-up` also detects this and redeploys) |
| `ERROR at teardown of <test>`: "cleanup failed after the test" | An undo step failed; the snapshot rollback still worked | Read `artifacts/<test-id>/cleanup-errors.txt`; usually a bug in the test's undo action |
| Run stops: "lab restore failed after <test>" (exit code 3) | The rollback itself failed; the lab is in an unknown state | `make lab-reset`, or `make lab-fresh` if that fails too; then rerun |
| `snapshot <id> has an older file format`, or "was taken from intent ..." / "on backend ..." (CLI exit 1, `--snapshot` exit 4) | The snapshot is from another lab: an older netcheck, another intent or another backend. Nothing was changed | `make lab-fresh` for a new baseline, or pass a snapshot ID from this lab |
| Header says `FAULTS ARE APPLIED`, tests fail | A fault from a demo or experiment is still in the lab | `make lab-reset` |
| "lab tests share one lab and must run serially" | You ran with `-n` (pytest-xdist) | Drop `-n` |
| `make lab-up` redeploys "intent is now ..." | `intent/lab.yaml` changed since the lab was built (also happens once after upgrading to this version) | Expected: the lab must match the intent |
| `IntentError` when loading the intent | A value in `intent/lab.yaml` is invalid; every problem is listed | Fix the listed fields |
| "ping ... did not run" | A host could not run `ping` at all (container down, tool missing); this used to look like "isolated" | `make smoke`; `make lab-fresh` if a container is down |
| Isolation or OSPF tests fail right after a demo | A demo fault is still applied | `make lab-reset` |
| OSPF neighbor test fails just after deploy | Adjacencies still forming | Wait ~10 s and rerun; check with `vtysh -c "show ip ospf neighbor"` |
| `exec format error` when the lab starts | An image has no build for your CPU (e.g. arm64 on Apple Silicon) | Pick a multi-arch image in `backends.yaml`, then `make lab-fresh` |
| `permission denied ... docker.sock` | Your user is not in the `docker` group | `sudo usermod -aG docker $USER && newgrp docker` |
| `containerlab: command not found` | Containerlab not installed in this shell (on macOS: you are not inside the VM) | `orb -m netlab`, or `make install-clab` |
| Permission errors under `build/` on a Mac-mounted folder | File sharing between macOS and the VM | Copy the repo into the VM's home (`cp -r /Users/<you>/.../netcheck-lab ~/`) and run there |
| Timing SLA failures on a slow or busy machine | Shared CPU makes timings noisy | `NETCHECK_TIME_FACTOR=2` for timeouts, `NETCHECK_SLA_MODE=record` to record SLAs without failing |
| `make check` fails on mypy imports | Dev dependencies missing | `make install` (installs the type stubs) |
| `uv run` fails: "Querying Python at .../.venv/bin/python failed" | The Mac and the VM shared one `.venv` and the other side replaced it | Use `make` targets (each OS gets its own `.venv-linux` / `.venv-darwin`), or in the VM `export UV_PROJECT_ENVIRONMENT=.venv-linux` in `~/.bashrc` |

Useful commands when something looks wrong:

```bash
sudo containerlab inspect -t build/campus-mini/campus-mini.clab.yml   # which nodes run
docker exec clab-campus-mini-<node> ip -br link                        # interfaces on a node
docker exec clab-campus-mini-sw1 bridge vlan show                      # switch VLAN membership
docker exec clab-campus-mini-r1 vtysh -c "show running-config"         # router config
docker exec clab-campus-mini-r1 vtysh -c "show ip route"               # router routes
uv run python -m netcheck restore                                      # back to baseline
cat artifacts/<test-id>/logs/<device>.jsonl                            # every call a failed test made
```

The NVIDIA backend (`--backend nvidia`) is not runnable yet: deploy raises
`NotImplementedError` (see Status). When built, it will need `NVIDIA_AIR_TOKEN` and
`NVUE_PASSWORD` in the environment and an SSH key in your agent.

## Writing a lab test

Write the test as steps a reader can follow: look up what the intent expects, read or change
the lab, check. Each step is a `with step("..."):` block whose title says what happens, with the
real values in it.

```python
def test_vlan_membership_matches_intent(lab: Lab, switch_name: str) -> None:
    switch = lab[switch_name]

    with step(f"Read from the intent which VLANs each {switch_name} port must carry"):
        expected = lab.expected.vlan_membership(switch_name)
        # e.g. {'p1': PortVlans('access', (10,)), 'p4': PortVlans('trunk', (10, 20))}

    with step(f"Read the live VLAN membership from {switch_name}"):
        live = switch.bridge.vlan_membership()

    for port, spec in sorted(expected.items()):
        intended = describe_vlans(spec.mode, spec.vlans)
        with step(f"Check {switch_name}:{port} is {intended}", continue_on_failure=True):
            check_port_vlans(switch_name, port, expected, live)

    with step(
        f"Check {switch_name} has no bridge ports the intent does not list",
        continue_on_failure=True,
    ):
        check_no_extra_ports(switch_name, expected, live)
```

`continue_on_failure=True` makes a **soft step** (an idea taken from pyATS, see ADR-0006): a
failed check is recorded as `[FAILED, continued]` and the next port is still checked. When the
test ends it fails with one report that lists every bad port. The last step catches a port
someone added to the bridge outside the intent (the backend reports it under its real name,
such as `eth9`). Use soft steps only for independent checks that just read and compare; a
setup step that fails should stop the test.

Rules that keep the suite honest:

- Get expectations from the intent: `lab.expected` answers questions in the capability types
  (`vlan_membership(switch)`, `routes(router)`, `failover()`, ...), never hard-coded names or
  addresses, so the test follows the intent.
- Read the lab only through capabilities (`device.ospf`, `.bridge`, `.links`, `.shell`).
- Check with the `check_*` / `assert_*` helpers in `netcheck.assertions`: they wait where the
  network needs time and build the failure report with evidence. A plain `assert` is fine for
  preconditions on intent data.
- Register an undo with `lab.cleanup.register(...)` right after any change you make.

## How it is built

For an interactive walk-through, open [`docs/backend-map.html`](docs/backend-map.html) in a browser:
click a device to see its ports and capabilities, or step through a test layer by layer.

```
intent/lab.yaml ──render──> build/<lab>/  <lab>.clab.yml, configs/, expected_state.json
                                               │
tests/lab/*  ──> assertions (assert_isolated, assert_ospf_neighbor_full, ...)
                    │                         └─> FailureReport (8 parts) ─> console, JUnit, artifacts/
                    └─> Device = capabilities (Ospf, Bridge, Links, Shell, Snapshot) over a transport
                              ├─ local:  docker exec + JSON (vtysh json, ip -j, bridge -j)
                              └─ nvidia: NVUE REST (JSON); hosts over SSH (planned)
```

| Path | What |
| --- | --- |
| `netcheck/intent.py` | intent model and validation |
| `netcheck/expected.py` | expected state derived from the intent, in the capability types; writes `expected_state.json` |
| `netcheck/render.py` | Containerlab topology, FRR config, switch and host setup |
| `netcheck/device.py`, `capabilities.py` | composition: devices hold capabilities; normalized state types |
| `netcheck/assembly.py`, `ports.py` | one table of capabilities per role; logical port to real name mapping |
| `netcheck/transport/` | `docker exec` and NVUE REST transports, each logging every call (`devicelog.py`) |
| `netcheck/backends/` | `base.py` (lifecycle, snapshots), `local` (Containerlab), `nvidia_air` (DSX Air) |
| `netcheck/faults.py` | demo faults, each breaking one intent |
| `netcheck/steps.py` | named test steps; `continue_on_failure=True` for soft steps that report every failed check in one run |
| `netcheck/assertions/` | domain assertions that collect evidence before failing: `l2.py` (VLANs, isolation), `reachability.py`, `ospf.py`, `common.py` (guarded evidence, captures) |
| `netcheck/labstate.py` | baseline, active faults and deployed-intent markers in `.netcheck/` |
| `netcheck/convergence.py` | traffic-based reconvergence measurement |
| `netcheck/cleanup.py` | LIFO cleanup registry |
| `netcheck/timing.py`, `timing.yaml` | thresholds derived from protocol timers |
| `netcheck/report.py` | eight-part failure report |
| `netcheck/testrun.py` | one lab test's run: snapshot, steps, cleanup, restore, failure evidence |
| `netcheck/pytest_plugin.py` | session lab; turns each test run's outcome and report into pytest results |
| `tests/unit/` | unit tests over fake transports and a fake lab (`fakes*.py`, NVUE samples in `fixtures/nvue/`) |
| `tests/unit/golden/`, `tests/unit/intents/` | reviewed render output per intent, and the test-only second intent (see Golden render) |
| `mutation/lab/` | opt-in mutation-testing profile over the lab suite |
| `scripts/install-clab.sh` | checked Containerlab install |

## Design decisions

See [`docs/decisions/`](docs/decisions/README.md) (and [`docs/backend-map.html`](docs/backend-map.html) for the interactive map):

1. [Two backends, one intent](docs/decisions/0001-backends-and-intent.md)
2. [Composition: transports and capabilities](docs/decisions/0002-device-composition-and-transports.md)
3. [Deploy once; snapshot + cleanup registry per test](docs/decisions/0003-lifecycle-and-isolation.md)
4. [Thresholds from protocol timers; SLA vs timeout](docs/decisions/0004-timing-and-thresholds.md)
5. [Failure reporting](docs/decisions/0005-failure-reporting.md)
6. [pytest, not pyATS; soft steps borrowed from it](docs/decisions/0006-pyats-considered.md)

## Timing

`timing.yaml` holds a timeout and an SLA per check, per backend profile, and can reference
intent timers (`dead_interval + 2`).

- `NETCHECK_TIME_FACTOR=2` scales timeouts on slow runners.
- `NETCHECK_SLA_MODE=strict` (default) fails on SLA breach; `record` (CI) records breaches as warnings and JUnit properties.
- `NETCHECK_NO_DEPLOY=1` makes a run use the running lab or fail with `LabDegraded`, never deploy (set by `make mutate-lab`).
- `NETCHECK_RESET_ON_START=1` restores the baseline when a run reuses the lab, instead of keeping what the last run left (set by `make mutate-lab`).

## Golden render

`render.py` writes each router's FRR config from the expected state, and the drift tests
compare the live lab with that same expected state, so a wrong assumption shared by both
would pass every drift test. Two unit test files break that loop, for every intent in
`intent/lab.yaml` and `tests/unit/intents/`:

- `tests/unit/test_golden.py` compares the render (topology, FRR configs,
  `expected_state.json`) with files in `tests/unit/golden/<intent>/` that a person has read.
  Any change to what would be deployed fails here first, with a diff.
- `tests/unit/test_render_roundtrip.py` reads the render back the way the devices would: FRR
  config through the local backend's parser, switch commands through a small model of the
  Linux bridge, host and router commands as iproute2 applies them, and compares the result
  with the intent.

`tests/unit/intents/two-site.yaml` is a second shape (three routers, two switches, a VLAN
spanning both, other timers) that is rendered and checked but never deployed. The real
`tests/lab` files also run on a healthy fake lab built from it, and all pass. A lab test
that only works on campus-mini's shape would fail there; `TWO_SITE_ASSUMES_CAMPUS_MINI`
(`tests/unit/test_review_fixes.py`) records any such test with the reason, and is empty.

After an intended change to rendering, an intent or an image:

```bash
make golden                       # rewrite tests/unit/golden from the current render
git diff tests/unit/golden        # read it: this diff is the review
```

The intent digest is replaced by a placeholder in the golden files, so editing a comment in
an intent does not touch them.

## Mutation testing

Mutation testing checks that the tests would notice a bug: mutmut changes the code one small
edit at a time and reruns the tests against each change.

```bash
make mutate                         # over tests/unit (slow; no lab needed)
make lab-up && make mutate-lab GROUP=FrrOspf   # opt-in, against the running local lab
```

`GROUP` is one of `LinuxShell`, `FrrOspf`, `links-bridge`, `backend`, `all`. The lab profile
never deploys and starts every mutant from the baseline. It never runs in CI. How to read
and triage the results, and the recorded baselines, are in
[`.claude/skills/mutation-testing/SKILL.md`](.claude/skills/mutation-testing/SKILL.md).

## Development workflow

New features are specified before they are built: requirements in
[`docs/requirements/`](docs/requirements/), then a step-by-step implementation plan in
[`docs/plans/`](docs/plans/), then implementation, review and a check that the plan is
complete. Claude Code agents, skills and slash commands for each stage are in
[`.claude/`](.claude/agents/README.md). Commits and PRs follow
[`.claude/skills/git-workflow/SKILL.md`](.claude/skills/git-workflow/SKILL.md); every change
passes `make check`.

## Status

- Framework core, intent rendering, assertions, reports, cleanup and timing: done, unit-tested with fake devices.
- Local backend: verified end to end on a real lab (Containerlab 0.76, FRR 10.1, Apple Silicon via OrbStack) and in GitHub Actions (`lab` job), including the code-review fixes. Lab mutation runs found and closed two isolation gaps: link restores are now verified, and a mutation run never deploys and starts every mutant from the baseline. Next: pin the switch and host image by digest with `make pin-images`.
- NVIDIA backend, device side: **unverified**. NVUE responses are parsed into the same normalized types as FRR, and config changes go through the NVUE revision flow, but 0 of 10 NVUE samples captured from a device: they were written from the NVIDIA docs (`tests/unit/fixtures/nvue/provenance.yaml`). The smoke, OSPF and drift lab tests pass on them unchanged, which shows the parsers agree with that reading of the docs, not with a switch. Unit runs print this count in their header and mark those tests `documentation_samples`; `record_samples(..., provenance_root=...)` on a DSX Air simulation replaces them and updates the count.
- NVIDIA backend, lab side: not built yet. It is specified in [`docs/requirements/nvidia-backend-requirements.md`](docs/requirements/nvidia-backend-requirements.md) and planned in [`docs/plans/2026-11-08-nvidia-backend-implementation-plan.md`](docs/plans/2026-11-08-nvidia-backend-implementation-plan.md): spikes on a real DSX Air simulation, then the Air API client, topology rendering, SSH access to devices through the `oob-mgmt-server`, NVUE snapshots, live runs with captured samples and a measured timing profile.
