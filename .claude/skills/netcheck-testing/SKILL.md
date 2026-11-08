---
name: netcheck-testing
description: "How netcheck is tested: unit tests over scripted transports and fake labs (tests/unit), lab tests with steps against a running lab (tests/lab), pytester plugin tests. Use when writing or extending tests for a parser, adapter, backend lifecycle, assertion, failure report or lab behaviour."
---

# Testing netcheck

Two suites, two purposes:

| Suite | Runs on | Proves | Command |
| --- | --- | --- | --- |
| `tests/unit` | nothing (fakes) | code is right: parsers, adapters, lifecycle rules, reports | `make test-unit` (in `make check`) |
| `tests/lab` | a deployed lab | the **network** follows the intent | `make test-lab [BACKEND=nvidia]` |

Every new code path gets a unit test. Lab tests change only when the intent or the
behaviour under test changes, never to make a backend pass.

## Unit tests: pick the seam

| Code under test | Fake to use | Where |
| --- | --- | --- |
| Local adapter (FRR, Linux bridge/links/shell, `LocalSnapshot`) | `ScriptedTransport(replies, default)`: replies per exact command; unscripted commands are recorded in `.writes` | `tests/unit/fakes_local.py` |
| NVUE adapter, revision flow | `FakeNvueTransport(device)`: GETs from `fixtures/nvue/<device>/<name>.<rev>.json`, config changes in `.changes`, `status_for` and `apply_state` force errors | `tests/unit/fakes_nvue.py` |
| Air API client | `FakeAirTransport` (scripted states, `status_for`) | `tests/unit/fakes_air.py` (NVIDIA plan, task A2) |
| SSH transport and tunnels | inject `runner` / `popen` / `connect` callables; assert argv | `tests/unit/test_ssh_transport.py` (task C1) |
| Lifecycle (`Backend.up`, reset, snapshots), assertions, reports | `build_fake_lab(intent, tmp_path)` / the `fake` fixture: a `FakeBackend` with `FakeOspf`, `FakeBridge`, `FakeLinks`, `FakeShell`, `FakeSnapshot` and a `FakeWorld` for pings | `tests/unit/fakes.py`, `conftest.py` |
| The pytest plugin end to end | `pytester` (enabled in `tests/unit/conftest.py`) running real lab test files on fake devices | `test_nvue.py::test_lab_tests_pass_unchanged_on_nvue_responses` |

The real transports (`docker_exec.py`, `nvue_rest.py`, and the new `rest.py`, `ssh.py`
internals that spawn processes) are reached only through injected callables; no unit test
opens a socket, runs `docker` or `ssh`, or calls Air.

## Unit test rules

- Name the behaviour: `test_link_still_down_after_restore_raises`, not `test_restore_2`.
  A docstring says why when the rule comes from an ADR or a past bug.
- Assert what was **sent** (commands in `.writes`/`.commands`, NVUE `.changes`) and what was
  **returned** (normalized types), not that a fake answered what it was told.
- One behaviour per test; parametrize shapes (`@pytest.mark.parametrize("parser, body", ...)`).
- Use the real intent (`intent` fixture loads `intent/lab.yaml`) and `Expected(intent)` as
  the oracle; don't hand-copy expected values that `Expected` can compute.
- Cross-backend contract tests: a backend's parser of OSPF interfaces and bridge membership
  equals `Expected(intent)`; config written for a NOS round-trips through that NOS's parser.
- Error paths are tested on purpose: unknown shapes raise, HTTP errors raise
  `TransportError`, a restore that does not take raises, a ping that did not run returns
  `error` (never "no replies").
- Time is injected (`sleep=`, `clock=` parameters); tests never sleep.
- Type the test (`-> None`); the `intent` fixture param carries
  `# type: ignore[no-untyped-def]` as the existing tests do.

## Lab tests

```python
pytestmark = pytest.mark.resilience  # smoke | functional | resilience | drift


def test_primary_link_failure_reroutes_via_backup(lab: Lab) -> None:
    """One sentence: the intent this test proves."""
    with step("Find the primary and backup links in the intent"):
        plan = lab.expected.failover()  # the oracle is Expected, not literals
    with step(f"Take {plan.router}:{plan.primary_port} down"):
        router.links.set_link(plan.primary_port, up=False)
        lab.cleanup.register(
            "bring it back up", lambda: router.links.set_link(plan.primary_port, up=True)
        )
```

- Tests use `lab[...]` devices, capabilities and `netcheck.assertions` only; no backend
  import, no NOS syntax, no port names other than logical ones.
- Every change a test makes registers its undo in `lab.cleanup` at the moment it is made;
  the per-test snapshot restore then catches anything left.
- Waits go through `lab.timing` and the assertions; negative checks run their full window.
- `step(..., continue_on_failure=True)` for checks that should all be reported together.
- Never run lab tests in parallel (xdist is refused: one lab).

## Running

```bash
make check                                    # ruff + mypy + unit tests, the gate for every change
uv run pytest tests/unit/test_nvue.py -q      # one file
make lab-up && make test-lab                  # local lab (Linux / the OrbStack VM)
make test-lab BACKEND=nvidia                  # needs NVIDIA_AIR_TOKEN, NVUE_PASSWORD
```

To check that new tests would catch a bug, use the `mutation-testing` skill on the
module you changed.
