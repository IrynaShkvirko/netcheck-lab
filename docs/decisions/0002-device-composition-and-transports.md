# ADR-0002: Devices are composed of transports and capabilities

- Status: Accepted
- Date: 2026-11-02

## Context

The same test (for example "OSPF neighbor r1-r2 is Full") must run against FRR in a
container and against Cumulus over the NVUE REST API. The original preference was
REST as the transport, but FRR containers and Linux hosts have no REST API, and
packet capture always needs a shell.

## Options

1. Inheritance: `Device` -> `Router` -> `FrrRouter`, `CumulusRouter`. Easy to
   explain, but every new NOS duplicates a class tree, and a device that is both a
   router and a switch does not fit.
2. Composition: a device *has* a transport and a set of capabilities.

## Decision

Composition.

```
Device(name, role, capabilities={...})
  capability  -> uses a transport -> writes to the device log
  Ospf          neighbors(), routes()
  Bridge        vlan_membership()
  Links         set_link(port, up)
  Shell         run(cmd), ping(), capture()
  Snapshot      take() / restore_to(state)   (implemented 2026-11-07, see ADR-0003)
```

**Resolved open question: transport choice is per backend and per device, not global.**

| Device | Local backend | NVIDIA backend |
| --- | --- | --- |
| Router / switch config and state | `docker exec` + JSON (`vtysh ... json`, `ip -j`, `bridge -j`) | NVUE REST (JSON) |
| Hosts, ping, tcpdump | `docker exec` | SSH via the Air out-of-band network |

REST is used wherever the NOS offers it. The Transport interface is two narrow
protocols: `CommandTransport.run(cmd)` and `ApiTransport.request(method, path, body)`.

- Parsing uses structured JSON output only. No regex over CLI text.
- Tests call domain assertions (`assert_isolated(h1, h3)`), which call capabilities.
- A missing capability raises `CapabilityMissing`, which the test layer turns into
  a skip with the backend name in the reason.

## Consequences

- New NOS = new capability implementations; tests and assertions are unchanged.
- Every transport call is recorded in a per-device log with request, raw response,
  return code and duration. Reports read from this log (ADR-0005).

## Revision (2026-11-03, code review)

The review found that assertions parsed FRR `running-config` text to explain OSPF failures,
which broke the rule above and would have made every diagnosis empty on the NVIDIA backend.

- New normalized capability methods: `Ospf.interface_settings()` returns `OspfInterface`
  (area, hello/dead interval, passive, cost, addresses) per logical interface (`p2`, or
  `p1.10` for a VLAN sub-interface), and `Ospf.set_interface_area()`.
- `running_config()` is no longer part of the `Ospf` interface; it stays inside the local
  backend for snapshots. FRR text is parsed in one place (`parse_frr_interfaces`).
- Faults go through capabilities too (the missing-OSPF fault uses `set_interface_area`).
- `Links.link_states(ports)` reads every port of a device in one call.
- A unit test fails if NOS syntax (`vtysh`, `running_config`, `ip ospf area`) appears in
  `netcheck/assertions/`.
