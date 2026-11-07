# netcheck

Tests that a lab network behaves as its intent says, on more than one backend.

## Language

**Intent**:
The description of what the network must do (`intent/lab.yaml`), and the single source of truth for topology, configuration and expected state. Identified by its digest.
_Avoid_: spec, topology file

**Snapshot**:
The saved configuration and link state of every device in the lab, tied to the intent it was taken from. Only restorable onto a lab built from that same intent.
_Avoid_: checkpoint, backup

**Baseline**:
The snapshot taken right after a real deploy; the known-good state with no faults in it.
_Avoid_: golden config, initial state

**Test run**:
One lab test from the snapshot taken before it to the restore after it, with everything that belongs only to that test: its steps, cleanup actions, evidence and artifacts.
_Avoid_: test context, test session (a session is the whole pytest run)

**Step**:
A named, numbered part of a lab test whose outcome is kept, so a failure report says which step failed and which passed before it.
_Avoid_: stage, phase

**Soft step**:
A step whose failed check is recorded while the test continues; the test fails at the end with every soft failure in one report.
_Avoid_: non-fatal assertion, soft assert

**Degraded lab**:
A running lab that no longer matches its deployment (for example, links lost after a container restart), so it must be redeployed rather than restored.
_Avoid_: broken lab
