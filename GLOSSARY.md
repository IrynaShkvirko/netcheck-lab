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

**Degraded lab**:
A running lab that no longer matches its deployment (for example, links lost after a container restart), so it must be redeployed rather than restored.
_Avoid_: broken lab
