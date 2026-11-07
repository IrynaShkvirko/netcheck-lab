# Triaging mutmut survivors in netcheck

Detail for [SKILL.md](../SKILL.md). Use it once a run has more survivors than you can read
one by one, which is normal on a first run.

**The survivor count is not the finding.** One missing assertion can leave dozens of
mutants alive. Triage by the *behaviour* left unasserted, have the claimed source bugs
refuted by someone else, then let a mutmut re-run decide whether the new tests worked.

| Stage | Who | Output | Gate |
|---|---|---|---|
| 1. Triage by unasserted behaviour | You, or one read-only agent per group of related modules | One finding per behaviour, classified | — |
| 2. Adversarial verification | One skeptic per claimed source bug, not the triager | held / downgraded / refuted | A reproduction, or the claim is refuted |
| 3. Test writing | Writers, with a reviewer re-running a sample of the hand proofs | New tests | A mutmut re-run over the same mutants |

## Stage 1: triage by unasserted behaviour

Group survivors by module family, for example:

| Group | Modules |
|---|---|
| Parsers | `backends/local.py` (`parse_*`, `frr_config_diff`, `host_route_diff`), `backends/nvidia_air.py` (`parse_nvue_*`) |
| Intent and expected state | `intent.py`, `expected.py`, `ports.py`, `render.py` |
| Assertions and reports | `assertions/*`, `report.py`, `steps.py`, `testrun.py` |
| Lifecycle | `backends/base.py`, `labstate.py`, `cleanup.py`, `timing.py`, `pytest_plugin.py` |

For each survivor read the mutant (`uv run mutmut show <name>`), the function, and the tests
that reach it (`uv run mutmut tests-for-mutant <name>`). Report **one finding per behaviour,
not one per mutant**, as one of:

| Class | Meaning | Action |
|---|---|---|
| Test gap | The code is right; no test asserts this behaviour | Stage 3 writes the test |
| Source bug | The mutant survives because the code is already wrong | Stage 2 first |
| Lab-only | Only `tests/lab` reaches it (real commands, `docker ps`, Air calls) | Check it with the lab profile (SKILL.md § "Lab profile"); deploy, destroy and NVIDIA code stay recorded as lab-only |
| Dead code | Unreachable or unused | Delete it rather than test it |

### Dismiss as noise

| Noise | Why it is not a finding |
|---|---|
| Human-facing wording: report and evidence lines, `print` messages, docstrings | Asserting prose makes tests brittle. Exception: verdicts and drift lines tests deliberately assert on |
| Equivalent mutants (`<` vs `!=` on a loop stepping by one, reordering commutative arguments) | No test can kill them |
| Defaults no test varies (timeouts, retry counts) | Only matter on a real lab |

### The first thing to look for here: asserting the fake, not the call

netcheck tests run real code over fakes (`FakeOspf`, `FakeBridge`, `ScriptedTransport`,
`FakeNvueTransport`). The common gap is a test that checks what a fake *returned* and never
what the code *sent*:

```python
# Weak: passes whatever command or payload the adapter builds
t = ScriptedTransport({"bridge -j vlan show": [data]})
LinuxBridge(t, ports).set_access_vlan("p3", 20)
assert LinuxBridge(t, ports).vlan_membership()  # reads the canned reply

# Strong: asserts what reached the device
assert t.writes == [
    "bridge vlan del dev eth3 vid 10",
    "bridge vlan add dev eth3 vid 20 pvid untagged",
]
```

For NVUE the equivalent is asserting `FakeNvueTransport.changes`, not the parsed state.

## Stage 2: adversarial verification of every claimed source bug

A "source bug" is a claim about production behaviour. Give each one to a skeptic that did
not do the triage, with this brief:

- Refute it. Default to **refuted** unless you produce a reproduction: an input or state,
  the observed output, the expected output.
- Scratch runs are fine (a throwaway test under `tmp/`, deleted afterwards). Do not edit
  source or real tests.
- Check it is reachable: from the lab tests, the CLI, the pytest plugin, or a fault.
- Return held, downgraded (real but lower impact, or a test gap) or refuted.

## Stage 3: a mutmut re-run is the gate

1. **Writers prove each kill by hand** (apply the mutation, show the new test failing with a
   message naming the behaviour, revert), following [hand-proofs.md](hand-proofs.md).
2. **A reviewer re-runs a sample** of those hand proofs instead of reading the writer's table.
3. **Re-run mutmut on the same mutants**: `uv run mutmut run "<glob of the targeted names>"`.
   That catches tests that kill the hand-applied mutation but not mutmut's actual one.
