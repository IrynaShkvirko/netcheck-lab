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
| Reconvergence, local link down | 0.2 s (2 s until 2026-11-10) | 10 s | traffic |
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
6. **Every measurement goes through `Timing`** (added 2026-11-07): values with an SLA through
   `record` (an unknown check name fails), values without one, such as the control-plane
   reconvergence time, through `observe`. `measure_reconvergence` only measures; the test
   records the data-plane outage against its SLA as a step of its own (ADR-0006).

## Consequences

- Every measured value lands in JUnit `<properties>`, so trends can be charted later.
- The NVIDIA profile will need its own numbers once Air boot times are measured.

## Revision (2026-11-03, code review)

- Reachability has its own entry (`reachability: timeout 15 s, poll 0.5 s` locally) instead
  of borrowing `route_installed`.
- The failover test now checks the starting path: before the failure, traffic must use the
  primary next hop. Without that, a lab already on the backup path "converged" in 0 s.

## Revision (2026-11-10, SLA as a gate)

A review found the failover SLA gated nothing: CI ran in `record` mode, the 2 s SLA sat 30x
above the measured 0.06 s outage, nobody read the recorded numbers, and two "SLAs" timed a
poll on a lab that had long converged.

- **Outage from the achieved send rate.** `ping` reports its elapsed time; outage = lost
  probes x (elapsed / gaps), not x the 10 ms asked for. On a busy machine the stream sends
  slower and each lost probe stands for more time; the nominal interval under-reported the
  outage. The achieved interval is the measurement's resolution and is observed
  (`reconverge_local_link_stream_interval`). BusyBox prints no time: then the asked interval.
- **Median of repeated runs.** `NETCHECK_REPEATS=N` runs the failover N times (the link comes
  back and the route returns to the primary in between); each run is observed
  (`reconverge_local_link_run`) and the median is held to the SLA, so one noisy run neither
  fails nor passes the check. Default 1.
- **Poll durations are not measurements.** `ospf_neighbor_full` and `route_installed` lose
  their SLA and are no longer recorded: on a converged lab they time the first `docker exec`.
  They remain timeouts.
- **History and summary.** Each lab session writes `artifacts/measurements.json` and appends it
  to `NETCHECK_HISTORY` (default `.netcheck/measurements.jsonl`). `python -m netcheck summary`
  prints this run next to the median of earlier runs of the same backend and intent; both CI
  jobs put it in the job summary. Fault-check sessions keep their own history.
- **Nightly gate.** `.github/workflows/nightly.yml` runs the lab suite in `strict` mode with
  `NETCHECK_REPEATS=5`, the history carried in the Actions cache. PR runs stay in `record`.
- **The SLA comes from measurements**: about three times the p95 of the per-run outages,
  rounded up, then revisited when the nightly history shows the CI runners' numbers.
  First set from 10 runs on the local lab (Containerlab 0.76, FRR 10.1, OrbStack on Apple
  Silicon, `NETCHECK_REPEATS=10`, strict): outage 0.012-0.061 s per run, median 0.012 s
  (1 to 5 lost probes); achieved stream interval 12 ms, not the 10 ms asked for, so the old
  formula under-reported every outage by 20%; control plane 0.049-0.060 s. With 10 runs the
  p95 is the maximum, 0.061 s: 3 x 0.061 rounded up gives **0.2 s**, down from 2 s. If the
  nightly median on GitHub's runners sits near it, raise it from that history, not by
  feel; `NETCHECK_TIME_FACTOR` never scales SLAs.
