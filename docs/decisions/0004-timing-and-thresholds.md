# ADR-0004: Thresholds derived from protocol timers; convergence measured with traffic

- Status: Accepted
- Date: 2026-11-02

## Context

Network tests wait for things to converge. Fixed `sleep` calls are slow and flaky.
Arbitrary timeouts hide real regressions or fail on slow CI runners.

## Decision

### Protocol timers are set in the intent

FRR defaults (hello 10 s, dead 40 s, DR election wait on broadcast links) are too slow
for a demo. The intent sets:

- `network: point-to-point` on router links (no DR election),
- `hello_interval: 1`, `dead_interval: 4`.

### Two numbers per check

- **timeout**: polling gives up and the test fails.
- **SLA**: the measured value that is asserted and recorded.

Values live in `timing.yaml`, per backend profile, and may reference intent timers
(`dead_interval + 2`), so changing a timer moves the threshold with it.

| Check | SLA | Timeout | Poll |
| --- | --- | --- | --- |
| Device ready (container) | - | 60 s | 2 s |
| Device ready (VM / Air) | - | 300 s | 5 s |
| Interface oper-up | 5 s | 30 s | 1 s |
| OSPF neighbor Full | 10 s | 30 s | 1 s |
| Route installed after Full | 5 s | 15 s | 0.5 s |
| VLAN change applied | 5 s | 15 s | 1 s |
| Reconvergence, local link down | 2 s | 10 s | traffic |
| Reconvergence, remote failure, no BFD | dead_interval + 2 | 15 s | traffic |
| Reconvergence with BFD (300 ms x 3) | 1.5 s | 5 s | traffic |
| Isolation (negative check) | 0 replies | fixed 4 s window | - |

### Rules

1. **Data-plane convergence is measured with traffic**, following the loss-derived
   method of RFC 6413: ping every 10 ms through the failure, outage = lost packets x
   interval. RIB polling is recorded as a second, control-plane number.
2. **Negative checks never pass early.** Isolation runs its full window and flushes
   neighbor caches first.
3. **`NETCHECK_TIME_FACTOR`** scales timeouts (not SLAs) on slow runners.
4. **Timeouts always fail. SLAs fail locally (`NETCHECK_SLA_MODE=strict`) and are
   recorded as warnings plus JUnit properties on shared CI runners
   (`NETCHECK_SLA_MODE=record`)**, where timing is noisy. This keeps CI green on
   noise while keeping the trend visible.
5. **A device that does not answer while polling is "not yet", not an error**
   (added 2026-11-07). A `TransportError` from a polling probe is retried until the
   timeout; if time runs out, the last error is the timeout's value and the failure
   report says "no answer: <error>". Other exceptions still stop the test at once.

## Consequences

- Every measured value lands in JUnit `<properties>`, so trends can be charted later.
- The NVIDIA profile will need its own numbers once Air boot times are measured.

## Revision (2026-11-03, code review)

- Reachability has its own entry (`reachability: timeout 15 s, poll 0.5 s` locally) instead
  of borrowing `route_installed`.
- The failover test now checks the starting path: before the failure, traffic must use the
  primary next hop. Without that, a lab already on the backup path "converged" in 0 s.
