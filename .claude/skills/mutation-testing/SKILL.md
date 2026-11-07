---
name: mutation-testing
description: "Mutation testing for netcheck with mutmut over tests/unit, plus an opt-in lab profile over tests/lab. Use when checking whether the unit tests would catch a bug in netcheck code, finding weak or missing assertions, triaging mutmut survivors, or proving a new test kills a mutant."
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
---

# Mutation testing (netcheck)

Adapted from the `mutation-testing` skill in
[laurigates/claude-plugins](https://github.com/laurigates/claude-plugins) (MIT, see
`LICENSE`), reduced to Python/mutmut and rewritten for this repo.

mutmut changes netcheck's code one small edit at a time (a mutant) and runs the unit tests
against each. A mutant the tests catch is **killed**; one they pass with **survived**, which
means no test asserts that behaviour. Coverage says a line ran; mutation testing says a test
would notice if it were wrong.

## When to use it

| Use it to | Not for |
|---|---|
| Check that tests around a parser, `Expected`, `PortMap` or a drift check really assert their output | Everyday lab behaviour checks: the lab profile is slow and opt-in (§ "Lab profile") |
| Find assertions that are too weak (checking a fake's reply instead of what was sent) | Writing tests from scratch (write them, then mutate) |
| Prove that a new test kills the mutant it targets | The two real transports, which are excluded (no unit test can reach them) |

## Setup (already done)

- `mutmut` is in the dev dependency group (3.8.1 when added; runs on Python 3.14).
- `[tool.mutmut]` in `pyproject.toml`: mutate `netcheck/` except
  `netcheck/transport/docker_exec.py` and `netcheck/transport/nvue_rest.py`; test with
  `tests/unit`; copy `intent/`, `timing.yaml`, `backends.yaml` and the root `conftest.py`
  into the run (mutmut copies `tests/` and `pyproject.toml` itself).
- mutmut works in `mutants/` (git-ignored): a mutated copy of the source plus the copied
  tests. Delete it to start from scratch.

## Running it

Use the project venv (`UV_PROJECT_ENVIRONMENT=.venv-darwin` or `.venv-linux`, as the
Makefile sets):

```bash
make mutate                                   # mutmut run + mutmut results
uv run mutmut run                             # every mutant (~5,500; ~4 min on a recent Mac)
uv run mutmut run "netcheck.ports.*"          # only some mutants (glob on names)
uv run mutmut results                         # survivors, timeouts, no-tests
uv run mutmut results --all true              # every mutant with its status
uv run mutmut show <name>                     # the diff of one mutant
uv run mutmut tests-for-mutant <name>         # the tests mutmut ran against it
uv run mutmut browse                          # interactive browser
```

`uv run mutmut apply <name>` writes the mutant into the **real** source file. Only use it
for a hand proof, and revert with `git checkout -- <file>` straight after.

### Mutant names

`<module>.x_<function>__mutmut_<n>` for a function, `<module>.xǁ<Class>ǁ<method>__mutmut_<n>`
for a method (the separator is `ǁ`, U+01C1):

```
netcheck.ports.x_port_name__mutmut_1
netcheck.ports.xǁPortMapǁreal__mutmut_7
```

### Statuses

| `mutmut results` | Meaning | Read as |
|---|---|---|
| killed | a test failed | good |
| survived | every selected test passed | a missing or weak assertion (triage it) |
| no tests | no unit test reaches the function | untested in unit runs; often lab-only code |
| timeout | the mutant made a test hang (an endless loop) | usually counts as caught |

## First-run control: do this before trusting any result

mutmut runs the tests from the copy in `mutants/`. Several tests find the repo through
`Path(__file__).parents[2]`, and the pytester tests put that path on `sys.path`. If the copy
were incomplete, or an import resolved to the installed package instead, every test would
run the **unmutated** code and every mutant would "survive". So first run one mutant that
must be killed:

```bash
uv run mutmut run "netcheck.ports.xǁPortMapǁreal__mutmut_1"   # base, dot, vid = None
```

It must report `🎉` (killed). If it survives, the run is broken, not the tests: check that
the files the tests read are listed in `also_copy`, then delete `mutants/` and re-run. The
same applies whenever `mutmut results` suddenly shows far more survivors than before.

## Baseline

First full run (2026-11-07): 5,476 mutants, **67.2% killed** (3,681), 1,380 survived,
402 no tests, 13 timeouts.

| Highest scores | | Lowest scores (modules over 50 mutants) | |
|---|---|---|---|
| `assembly` | 96% | `convergence` | 42% |
| `ports` | 91% | `__main__` | 44% |
| `expected` | 84% | `pytest_plugin` | 44% |
| `intent`, `backends.base` | 83% | `assertions.common` | 51% |

Most of the 402 no-tests mutants are lab-only code in `backends/local.py` and
`backends/nvidia_air.py` (deploy, destroy, `is_running`) and `get_backend`, which the tests
replace with fakes. There is no CI threshold yet; use the baseline to see whether a change
moved the score, not as a target.

## Reading a survivor

A real example from the first run:

```diff
-    return self._real.get(base, base) + (f".{vid}" if dot else "")
+    return self._real.get(base, None) + (f".{vid}" if dot else "")
```

`netcheck.ports.xǁPortMapǁreal__mutmut_7` survives: `PortMap.real()` promises to return a
name the intent does not map unchanged, and no test calls it with such a name. That is a
**test gap** (the code is right), fixed by one assertion:

```python
assert PortMap({"p1": "eth1"}).real("p9") == "p9"
```

One finding per behaviour, not per mutant: mutants 7, 8 and 9 of the same function are
all this one gap.

## Lab profile (tests/lab, opt-in)

The unit run cannot reach the local backend's real commands: captures, ping streams, FRR
reads, link and bridge changes, `docker ps`. The lab profile mutates exactly those and runs
`tests/lab` against a **running lab**:

```bash
make lab-up                          # a deployed, healthy lab first
make mutate-lab GROUP=FrrOspf        # LinuxShell | FrrOspf | links-bridge | backend | all
```

| Group | Mutants | Functions |
|---|---|---|
| `LinuxShell` | 131 | ping, captures, streams, host routes, neighbors |
| `FrrOspf` | 46 | neighbors, routes, LSDB, interface settings, area changes |
| `links-bridge` | 64 | link state and changes, VLAN membership and changes |
| `backend` | 79 | `LocalBackend` `is_running`, `environment`, `devices`, `open_transport`, `container` |
| `all` | 320 | all of the above |

Each mutant runs the lab tests that reach its function, so expect minutes per mutant, not
seconds; start with one group. The cost per mutant has not been measured yet.

**How it is set up.** mutmut only reads the config in the directory it runs from, so the
profile is the folder `mutation/lab/`: its own `pyproject.toml` (mutate only
`netcheck/backends/local.py`, test with `tests/lab`, longer timeouts) and symlinks to the
real tree. `make mutate-lab` runs mutmut from there; its working copy is
`mutation/lab/mutants/`, so the lab run never touches the unit run's results. To look at
lab results afterwards, run the same commands from that folder:
`cd mutation/lab && uv run --project ../.. mutmut results` (with `UV_PROJECT_ENVIRONMENT`
set to the absolute path of the venv).

**Safety rules, all built in:**

- **The lab must be green first.** `make mutate-lab` runs `make test-lab` and stops if it
  fails: a red lab turns every mutant into a false kill.
- **One mutant at a time** (`--max-children 1`): lab tests share one set of containers.
- **Lab isolation always runs real code.** Snapshot/restore (`backends/base.py`,
  `LocalSnapshot`), the lifecycle (`up`, `reset`, `labstate`), the plugin, `testrun` and
  `cleanup` are never mutated here; the unit run mutates them against fakes. `deploy` and
  `destroy` are left out too: `deploy` never runs on a reused lab, `destroy` tears it down.
- **The run uses a copy of the lab's state.** `.netcheck/` is copied into the working copy,
  so the run reuses the running lab and its baseline instead of redeploying, and anything
  it writes (snapshots, the fault ledger) stays in the copy.
- **Timing cannot cause a kill.** `NETCHECK_SLA_MODE=record` turns SLA breaches into
  warnings, so a slow mutant fails only on behaviour.

**Distrust timing-sensitive kills.** mutmut counts any failure as a kill, including a flaky
one. Re-run every mutant killed only by the failover, reconvergence or isolation tests
(`uv run --project ../.. mutmut run <name>` from `mutation/lab/`) before believing it.

**Not here:** the NVIDIA backend, which cannot deploy a lab yet. Never run the lab profile
in CI.

**Lab baseline:** not measured yet. Fill in the score per group after the first run.

## Many survivors

For a first run, or any run with more survivors than you can read one by one, follow
[references/triage.md](references/triage.md): group by module family, classify each
unasserted behaviour (test gap, source bug, lab-only, dead code), dismiss the noise listed
there, have a separate agent try to refute every claimed source bug, and use a mutmut
re-run on the same mutants as the gate for new tests.

## Proving a kill by hand

When you show a new test kills a mutant by applying the mutation yourself, the result can
lie in four ways (another check fails first, the mutation is visible to an earlier check,
derived state is stale, or the edited file is not the one imported). The rules and the
netcheck paths to watch are in [references/hand-proofs.md](references/hand-proofs.md).
