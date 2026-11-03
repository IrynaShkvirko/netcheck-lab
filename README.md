# netcheck-lab

An intent-driven network test framework. One YAML file describes what the network
must do; the framework builds the lab from it, tests the live network against it,
and explains every failure in terms of the intent that was violated.

It runs on two backends behind one interface:

- **local**: FRR routers, a Linux-bridge switch and Linux hosts in Containerlab. Runs on a laptop and in GitHub Actions.
- **nvidia**: Cumulus Linux on NVIDIA Air over the NVUE REST API (skeleton, see Status).

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

## What the tests prove

| Marker | Tests | Intent |
| --- | --- | --- |
| `smoke` | ports up, OSPF running | the lab is usable |
| `functional` | VLAN isolation, reachability, OSPF adjacency, learned routes | every pair and route listed in the intent |
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
curl -sL https://containerlab.dev/setup | sudo -E bash -s "all"   # Docker + Containerlab
sudo usermod -aG docker $USER && newgrp docker                     # docker without sudo
sudo apt-get install -y make
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc

cd /path/to/netcheck-lab
make install          # Python dependencies
make check            # lint + types + unit tests; no lab needed
```

`make check` should end with all unit tests passing. If it does not, fix that first: the
problem is the Python setup, not the lab.

### 3. Start the backend (the lab)

```bash
make lab-up
```

This renders the topology and configs from `intent/lab.yaml`, deploys the lab (first run
pulls images, a few minutes), and saves a **baseline snapshot**. If the lab is already
running and healthy it is reused; if it is running but degraded, it is redeployed.

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

Every test starts from a fresh snapshot and is rolled back afterwards, so tests can run in
any order and a failing test does not leave the lab broken.

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
3. The isolation test fails. This is intended; make prints `Error 1 (ignored)` and continues.
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
Full but the route disappears, and the report says so: "adjacencies are Full but the
prefix is not in the LSDB: r2 is not advertising 10.0.30.0/24", with the interface drift.
Run it with `make fault-missing-ospf && uv run pytest tests/lab -m functional`, then `make lab-reset`.

## Troubleshooting

Start with the error text: command failures name the container they ran on
(`clab-campus-mini-r1: 'ip -j link show eth1' failed ...`).

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Device "eth1" does not exist`, or `lab links are gone (r1:p1 (eth1))` | A container restarted (Docker restart, VM or laptop sleep) and lost its links; Containerlab wires links only at deploy | `make lab-fresh` (`make lab-up` also detects this and redeploys) |
| Tests pass but warn `cleanup errors in ...` | The per-test rollback hit a problem | Read `artifacts/<test-id>/cleanup-errors.txt`; if the lab looks wrong, `make lab-reset` or `make lab-fresh` |
| Isolation or OSPF tests fail right after a demo | A demo fault is still applied | `make lab-reset` |
| OSPF neighbor test fails just after deploy | Adjacencies still forming | Wait ~10 s and rerun; check with `vtysh -c "show ip ospf neighbor"` |
| `exec format error` when the lab starts | An image has no build for your CPU (e.g. arm64 on Apple Silicon) | Pick a multi-arch image in `backends.yaml`, then `make lab-fresh` |
| `permission denied ... docker.sock` | Your user is not in the `docker` group | `sudo usermod -aG docker $USER && newgrp docker` |
| `containerlab: command not found` | Containerlab not installed in this shell (on macOS: you are not inside the VM) | `orb -m netlab`, or `make install-clab` |
| Permission errors under `build/` on a Mac-mounted folder | File sharing between macOS and the VM | Copy the repo into the VM's home (`cp -r /Users/<you>/.../netcheck-lab ~/`) and run there |
| Timing SLA failures on a slow or busy machine | Shared CPU makes timings noisy | `NETCHECK_TIME_FACTOR=2` for timeouts, `NETCHECK_SLA_MODE=record` to record SLAs without failing |
| `make check` fails on mypy imports | Dev dependencies missing | `make install` (installs the type stubs) |

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

The NVIDIA backend (`--backend nvidia`) is not runnable yet; it needs `NVIDIA_AIR_TOKEN` and
`NVUE_PASSWORD` and raises `NotImplementedError` at deploy (see Status).

## How it is built

For an interactive walk-through, open [`docs/backend-map.html`](docs/backend-map.html) in a browser:
click a device to see its ports and capabilities, or step through a test layer by layer.

```
intent/lab.yaml ──render──> build/<lab>/  topology.clab.yml, configs/, expected_state.json
                                               │
tests/lab/*  ──> assertions (assert_isolated, assert_ospf_neighbor_full, ...)
                    │                         └─> FailureReport (8 parts) ─> console, JUnit, artifacts/
                    └─> Device = transport + capabilities (Ospf, Bridge, Links, Shell)
                              ├─ local:  docker exec + JSON (vtysh json, ip -j, bridge -j)
                              └─ nvidia: NVUE REST (JSON)
```

| Path | What |
| --- | --- |
| `netcheck/intent.py` | intent model, validation, expected state |
| `netcheck/render.py` | Containerlab topology, FRR config, switch and host setup |
| `netcheck/device.py`, `capabilities.py` | composition: devices hold capabilities |
| `netcheck/transport/` | `docker exec` and NVUE REST transports, each logging every call |
| `netcheck/backends/` | `local` (Containerlab) and `nvidia` (Air) |
| `netcheck/assertions.py` | domain assertions that collect evidence before failing |
| `netcheck/convergence.py` | traffic-based reconvergence measurement |
| `netcheck/cleanup.py` | LIFO cleanup registry |
| `netcheck/timing.py`, `timing.yaml` | thresholds derived from protocol timers |
| `netcheck/report.py` | eight-part failure report |
| `netcheck/pytest_plugin.py` | session lab, per-test snapshot + cleanup, report hook |

## Design decisions

See [`docs/decisions/`](docs/decisions/README.md) (and [`docs/backend-map.html`](docs/backend-map.html) for the interactive map):

1. [Two backends, one intent](docs/decisions/0001-backends-and-intent.md)
2. [Composition: transports and capabilities](docs/decisions/0002-device-composition-and-transports.md)
3. [Deploy once; snapshot + cleanup registry per test](docs/decisions/0003-lifecycle-and-isolation.md)
4. [Thresholds from protocol timers; SLA vs timeout](docs/decisions/0004-timing-and-thresholds.md)
5. [Failure reporting](docs/decisions/0005-failure-reporting.md)

## Timing

`timing.yaml` holds a timeout and an SLA per check, per backend profile, and can reference
intent timers (`dead_interval + 2`).

- `NETCHECK_TIME_FACTOR=2` scales timeouts on slow runners.
- `NETCHECK_SLA_MODE=strict` (default) fails on SLA breach; `record` (CI) records breaches as warnings and JUnit properties.

## Status

- Framework core, intent rendering, assertions, reports, cleanup and timing: done, unit-tested with fake devices.
- Local backend: verified end to end on a real lab (Containerlab 0.76, FRR 10.1, Apple Silicon via OrbStack). Next: pin images by digest with `make pin-images`.
- NVIDIA backend: transport and structure in place; NVUE object paths and Air simulation creation are open questions in ADR-0001 and ADR-0003.
