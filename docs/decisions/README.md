# Architecture decision records

| ADR | Decision | Open questions |
| --- | --- | --- |
| [0001](0001-backends-and-intent.md) | Two backends (local FRR + Containerlab, NVIDIA Air + NVUE) driven by one intent file with logical ports | Air topology creation and boot time |
| [0002](0002-device-composition-and-transports.md) | Devices composed of transports and capabilities; REST where the NOS offers it, exec/SSH elsewhere; JSON parsing only | - |
| [0003](0003-lifecycle-and-isolation.md) | Deploy once per session; per-test snapshot/rollback plus a LIFO cleanup registry | NVUE rollback via API |
| [0004](0004-timing-and-thresholds.md) | Thresholds derived from protocol timers; SLA vs timeout; traffic-based convergence (RFC 6413) | NVIDIA timing profile |
| [0005](0005-failure-reporting.md) | Eight-part failure report mapped to console, JUnit XML and artifacts | - |
| [0006](0006-pyats-considered.md) | pytest stays the runner; pyATS/Genie considered (no FRR, Cumulus or NVUE parsers); soft steps borrowed from it | Genie inside a future Cisco backend |

Format: context, options, decision, consequences, open questions. A superseded ADR is
kept and marked, not deleted.
