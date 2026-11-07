# Proving a kill by hand: four ways it misreports

Detail for [SKILL.md](../SKILL.md). Use it whenever you apply a mutation yourself to show a
new test catches it (triage Stage 3), or write any loop that mutates code and runs a check.
mutmut tells you which test killed a mutant; a hand proof only tells you *something* went
red, and that is not the same thing. §1–§3 give a false "caught", §4 a false "missed".

### 1. Another check fails first and masks the one you meant

netcheck assertions raise `IntentViolation` with a verdict that names the intent, and many
reports carry several checks. A mutation in `ospf_interface_drift` can make an earlier
adjacency check fail on the way past, so the test goes red for the wrong reason.

**Read the failure, never just the colour.** Match it on the verdict or drift line you
expect, for example `pytest.raises(IntentViolation, match="cost")` or an assertion on
`report.drift[0].where`. If the message does not name the check you are proving, the
mutation did not reach it.

### 2. Choose a mutation only the target check can see

Fixing §1 is not "mutate harder"; it is picking a mutation no earlier check intercepts.

| Proving | Bad mutation | Works |
|---|---|---|
| `ospf_interface_drift` reports cost | change the area too (the area line fires first) | change only `cost` |
| isolation fails on a shared VLAN | move the host off the switch (attachment lookup fails first) | put both hosts' ports in one VLAN |

The mutation is the independent variable; anything else it disturbs is a confound.

### 3. Patching a table leaves state built from it at import time stale

Modules often derive lookups from a table once, at import. `PortMap` builds its reverse
map in `__init__`; `Expected` computes the originated prefixes when it is created. Patching
the source data afterwards leaves the derived copy describing the old data:

```python
lab.intent.vlans[10] = replace(lab.intent.vlans[10], gateway_ip=None)
expected = Expected(lab.intent)  # REQUIRED: lab.expected still holds the old answers
```

**Rebuild every derived object, or build the fixture again from the changed input.** Grep
for what is computed from the thing you patched.

### 4. The mutated file is not the one that gets imported

The mirror of §1–§3: a mutation reported as missed although nothing ran it. mutmut runs
the tests from a copy under `mutants/`; a hand loop often stages a temp copy. If any import
path leads back to the real tree, tests run the unmutated code and every row looks like a
coverage hole.

In this repo the paths to watch:

| Second copy reachable via | Where |
|---|---|
| `Path(__file__).parents[2]` used as the repo root | `tests/unit/conftest.py`, `test_cleanup_timing.py`, the pytester tests |
| `sys.path.insert(0, repo)` in generated conftests | `test_review_fixes.py`, `test_nvue.py`, `test_lifecycle.py` pytester templates |
| `ROOT = Path(__file__).parent.parent` | `netcheck/pytest_plugin.py`, `netcheck/__main__.py` |
| an installed (non-editable) `netcheck` in the venv | `uv sync` builds the package into site-packages |

**The tell is the control.** A mutation that must be caught (a hard-wired wrong answer)
reporting "missed" is not a weak test; it proves the harness is not running the file it
edited. Always run the control first (SKILL.md § "First-run control").
