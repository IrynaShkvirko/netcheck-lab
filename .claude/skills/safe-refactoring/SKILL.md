---
name: safe-refactoring
description: "Behaviour-preserving refactoring of netcheck in baby steps: characterization tests first, one structural change per commit, make check after each. Use when restructuring backends, transports, assembly, assertions or the pytest plugin without changing what they do."
---

# Safe refactoring (netcheck)

A refactoring changes structure, not behaviour: the unit suite, the lab suite and the
failure reports must say the same things before and after. Feature work waits until the
refactoring is merged.

## Before the first step

1. `make check` is green and the tree is clean.
2. Characterize what the code does today, as tests over the fakes, for every behaviour the
   refactoring could change. Typical gaps in netcheck:
   - the exact commands or NVUE changes an adapter sends (`ScriptedTransport.writes`,
     `FakeNvueTransport.changes`);
   - error messages that end up in failure reports;
   - lifecycle decisions (`Backend.up` reuse/redeploy/baseline) and env-var behaviour.
3. Optional but cheap: run the `mutation-testing` skill on the module first; survivors are
   behaviours no test pins yet.
4. For lab-facing code, run `make test-lab` once and keep the JUnit file to compare.

## Steps

Each step is one structural change, summarizable in one sentence, green on its own, and its
own commit:

| Valid step | Example |
| --- | --- |
| Extract a pure function | `frr_config_diff` out of `LocalSnapshot._restore_frr` |
| Introduce a seam | adapters take `CommandTransport` instead of building `DockerExecTransport` |
| Move a module and re-export | `redact` moves to `transport/rest.py`, re-exported from `nvue_rest` |
| Rename with every caller | `NvueFactory` -> `NvidiaFactory`, alias kept until callers move |
| Replace a duplicate with the shared one | four port-map copies -> `PortMap.for_node` |

Not a step: "rewrite the backend", or an extract plus a rename plus a behaviour fix at once.

After each step: `make check`; for lab-facing code, the lab suite at the end of a track.

## Risks to check on every refactoring

- **Seam leaks:** NOS syntax moving into `netcheck/assertions/` or tests.
- **Cross-backend drift:** one backend's parser or config writer changed without the
  round-trip contract tests.
- **Lifecycle rules moved out of `Backend.up`/`reset`:** the CLI and the pytest session must
  keep calling the same methods.
- **Lost logging:** a transport path that no longer records in the device log.
- **Changed error text:** failure reports and docs quote it.
- **Import cycles:** `faults` and `backends` import late on purpose.

## Rollback

Every step is a commit: abandon by reverting the steps not yet merged; nothing half-moved
stays on `master`. Aliases kept for callers are removed in the last step only.
