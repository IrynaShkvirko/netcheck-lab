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

## Quick start

Requirements: Linux, Docker, [uv](https://docs.astral.sh/uv/), sudo for Containerlab.

```bash
make install         # Python deps
make install-clab    # Containerlab at the pinned version
make lab-up          # render + deploy once + baseline snapshot
make test-lab        # full suite, JUnit in artifacts/junit.xml
make lab-down
```

Without a lab: `make check` runs ruff, mypy and the unit tests (fake devices).

## Demo (3-5 minutes)

```bash
make demo
```

1. Smoke tests pass.
2. `make fault-wrong-vlan` moves h3's switch port into VLAN 10.
3. The isolation test fails with a report like this:

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
    10.0.20.13 lladdr aa:c1:ab:00:00:03 REACHABLE

Capture (summary):
  h3:p1
  IP 10.0.10.11 > 10.0.20.13: ICMP echo request
  IP 10.0.20.13 > 10.0.10.11: ICMP echo reply
  file: artifacts/<test>/h3-p1.pcap

Env: topology=campus-mini  backend=local  intent=61de9bdddff0  nos=FRRouting 10.1.0 ...
Repro: pytest '...test_hosts_in_different_vlans_are_isolated[h1-h3]' --backend local --snapshot <id>
Restore state: python -m netcheck restore <id>
```

4. `make lab-reset` restores the baseline; the failover test runs and reports the measured outage.

The second fault, `make fault-missing-ospf`, removes r2's LAN from OSPF. Adjacencies stay
Full but the route disappears, and the report says so: "adjacencies are Full but the
prefix is not in the LSDB: r2 is not advertising 10.0.30.0/24", with the interface drift.

## How it is built

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

See [`docs/decisions/`](docs/decisions/README.md):

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
- Local backend: implemented; first end-to-end lab run still to do (container image tags and FRR JSON field names to confirm on that run, then pin images with `make pin-images`).
- NVIDIA backend: transport and structure in place; NVUE object paths and Air simulation creation are open questions in ADR-0001 and ADR-0003.
