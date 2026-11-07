# ADR-0001: Two backends driven by one intent file

- Status: Accepted
- Date: 2026-11-02

## Context

The framework must test network behaviour (VLAN isolation, OSPF, failover) on a
NVIDIA network operating system, and it must also run on a laptop and in GitHub
Actions so a reviewer can reproduce every result.

Cumulus VX is discontinued (last version 5.12.1) and is no longer published as a
standalone image; NVIDIA points users to NVIDIA Air for Cumulus simulation.
A public repo that depends on an image nobody can download cannot be reproduced.

## Options

1. Cumulus only, through NVIDIA Air. Real NVUE REST API, but needs an Air account,
   network access and an API token. Not runnable offline or by a reviewer.
2. SONiC VS only. Free image, but the REST management plane in community VS
   builds is not dependable, and it is not the product line this role focuses on.
3. Two backends behind one interface: a local backend (FRR routers and a Linux
   bridge switch in Containerlab) and an NVIDIA backend (Cumulus on NVIDIA Air,
   NVUE REST).

## Decision

Option 3.

- **Local backend** is the default. It runs everywhere, including CI.
  FRR is the routing suite inside both Cumulus Linux and SONiC, so OSPF behaviour
  is representative.
- **NVIDIA backend** runs manually or nightly with `NVIDIA_AIR_TOKEN` set.
- Tests never know which backend they run on.

### Intent is the single source of truth

`intent/lab.yaml` describes what the network must do. From it the framework
generates three things:

1. the Containerlab topology,
2. device configuration (FRR, bridge, hosts),
3. the expected state that tests assert against.

**Resolved open question: backend-specific details stay out of the intent.**
Ports are logical (`p1`, `p2`, ...). Each backend maps them to real names
(`eth1` on the local backend, `swp1` on Cumulus). Nothing in the intent names a
vendor, image or interface.

### Update 2026-11-07: expected state is a typed module

The third output is computed by `netcheck.expected.Expected`. Tests and assertions ask it
questions (`vlan_membership(switch)`, `routes(router)`, `originator(prefix)`,
`attachment(host)`, `failover()`) and get the same normalized types the capabilities
return, instead of reading string keys from a dict. `expected_state.json` is its
`to_json()` and is unchanged.

`Expected.ospf_interfaces(router)` states each router's intended OSPF interfaces as
`OspfInterface` values (routed ports and VLAN gateway sub-interfaces). Render writes the FRR
config from it, the OSPF drift checks compare live state with it using one rule (area as a
number, timers, network type, cost, addresses), and a round-trip test requires the FRR
writer and parser to agree with it.

## Consequences

- Adding a NOS means writing one backend, not touching tests.
- Some features exist on one backend only (for example MLAG). Tests that need a
  missing capability are skipped with a clear reason, not failed (see ADR-0002).
- The NVIDIA backend is the riskiest part and is built second.

## Open questions

- Air API: confirm how a topology is created from a generated spec and how long a
  simulation takes to boot. This sets the NVIDIA timing profile (ADR-0004).
  Update 2026-11-06: the service is now NVIDIA DSX Air. Simulations are created with
  `POST https://api.dsx-air.nvidia.com/api/v3/simulations/import/` (JSON or DOT
  topology, NGC API key as Bearer token) and started with a separate call. Accounts go
  through an NGC organization; personal email domains are not accepted.
- Reaching NVUE: switch management addresses (192.168.200.0/24) are only reachable inside
  the simulation; external access ends at the `oob-mgmt-server`. Either run the tests
  there or tunnel the NVUE port through it.
- NVUE JSON: the RIB, bridge and link response shapes are not documented as JSON; the
  parsers follow the documented object model and fail on unknown shapes. Confirm with
  responses captured from a real switch (`record_samples`).
