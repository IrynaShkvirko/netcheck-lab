# ADR-0006: pytest stays the runner; pyATS considered, soft steps borrowed

- Status: Accepted
- Date: 2026-11-06

## Context

pyATS (Cisco's test framework, with the Genie library and the Unicon connection library)
is the best-known framework built for network testing. A typical pyATS project, such as
[hpreston/intro-network-tests](https://github.com/hpreston/intro-network-tests), has:

- a `testbed.yaml` inventory of devices, with credentials and connections;
- **Unicon**, which connects to each device over SSH or telnet and handles CLI prompts;
- **Genie** parsers and models: `device.parse("show ...")` and
  `device.learn("interface")` turn CLI output into structured data;
- **aetest**, its own test runner (CommonSetup, Testcase, CommonCleanup), with nested
  steps and an HTML log viewer;
- **easypy** job files to run several scripts and collect the results.

The question: should this project use pyATS instead of pytest plus its own plugin?

## Options

1. **pyATS + Genie + aetest instead of pytest.**
2. **pytest, with Genie parsers inside the backends** (pyATS as a parsing library only).
3. **pytest and our own JSON parsers** (the current design), borrowing pyATS ideas
   where they help.

## What decides it

**Platform coverage.** Genie's value is its parsers, and nearly all of them are for Cisco
operating systems (IOS-XE, NX-OS, IOS-XR, ASA). For the platforms in this project, the
genieparser repository (checked November 2026) has:

| Platform | Genie parsers |
| --- | --- |
| Linux | `ifconfig`, `route`, `ps`, `ls`, `curl`, `docker stats` |
| SONiC | `show version`, `show platform inventory`, transceiver EEPROM |
| FRR, Cumulus Linux, NVUE | none |

None of the state these tests check (OSPF neighbors and routes, bridge VLAN membership,
OSPF interface settings) is covered. We would write those parsers ourselves either way.
We already have them, and they read JSON (`vtysh ... json`, `bridge -j`, NVUE REST)
instead of scraping CLI text, as ADR-0002 requires.

**Connection model.** Unicon expects an SSH or telnet session with a CLI prompt. Our
transports are `docker exec` (local backend) and HTTPS REST (NVUE). Both would need
custom Unicon plugins or workarounds.

**Test runner.** aetest replaces pytest. We would lose what the project is built on:
the pytest plugin (session lab, per-test snapshot and cleanup, failure reports from a
report hook), fixtures and parametrization from the intent, markers, JUnit XML for CI,
and pytester tests that run the real lab tests against a fake lab. pytest is also what
most QA and SDET teams already know.

**What pyATS does well.** Its reports are step-based, and a step can be marked
`continue_=True`: the failure is recorded, the remaining steps still run, and the
test fails at the end. For checks of many independent items (every port, every router)
one run then lists every problem instead of stopping at the first.

## Decision

Option 3. pytest stays the runner, and the backends keep their own JSON parsers.

We borrow soft steps from pyATS:
`step(title, continue_on_failure=True)` in `netcheck.steps`.

- A failed check (`AssertionError`, which includes `IntentViolation`) is recorded on
  the step and the test continues. Any other error, `pytest.fail` and `pytest.skip`
  still stop the test, because later steps could not be trusted after them.
- When the test body ends, the pytest plugin fails the test with one `FailureReport`
  that combines all soft failures: the verdicts, drift, diffs, captures and evidence of
  each one (ADR-0005). The step list marks them `[FAILED, continued]` with the actual
  value.
- If a later step fails hard, that failure is the verdict; the earlier soft failures
  still show in the step list.

Used where the checks are independent: one step per switch port, plus one for bridge
ports the intent does not list, in `test_vlan_membership_matches_intent`, one step per router in `test_routers_run_ospf`.
Steps that depend on earlier ones (look up, configure, send traffic, check) stay
normal steps, because a failed setup step makes the rest meaningless.

## Consequences

- A drift run with two bad ports produces one failing test whose report lists both
  ports, instead of two runs.
- A soft step must not leave state that later steps depend on. Soft steps only read
  and compare.
- Tests do not depend on Cisco's libraries, which keeps installs small and avoids a
  runner most of the audience would have to learn first.

## Open questions

- If a Cisco backend is ever added (IOS-XE, NX-OS), Genie parsers could be used inside
  that backend's capabilities (option 2) without changing the tests.
- A pyATS-style HTML log viewer is not planned; JUnit XML plus `report.txt` and
  `report.json` per failed test cover CI and local debugging.
