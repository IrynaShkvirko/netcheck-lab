---
name: brainstorming
description: "Turn a rough netcheck idea (a new test, capability, backend, NOS, fault or report change) into an agreed design before any code. Use when starting new work whose shape is not settled yet."
---

# Brainstorming a netcheck change

Design first, code second. The output is a short design document the user agreed to,
section by section.

## Rules

- **One question per message**, multiple choice when you can (2–3 concrete options).
- **Two approaches minimum** before deciding, each with its cost to the seam (does it touch
  tests or assertions? the intent? one backend or all?).
- **YAGNI.** The intent describes a small campus network; features the intent does not need
  (MLAG, EVPN, BGP) stay out unless the user asks.
- Sections of 200–300 words; wait for agreement on each.

## Steps

1. **Read the ground.** `GLOSSARY.md`, the ADRs touching the area (`docs/decisions/`),
   `README.md` Status, and the code the idea would change. Summarize the current state in
   2–3 sentences. Done when the user confirms the summary.
2. **Find the intent.** Ask until these are answered: which intent (or which kind of
   failure) does this make testable or diagnosable? On which backends? What does a failure
   report need to say? Is there already a capability, assertion or fault close to it?
3. **Propose approaches.** For each: how it works (1–2 sentences), which layer changes
   (intent / `Expected` / capability / adapter / assertion / test / report), pros, cons,
   and whether it contradicts an ADR (say so explicitly).
4. **Design in sections**, each validated before the next:
   - **Intent and expected state**: new intent fields (logical, NOS-free) and what
     `Expected` derives from them.
   - **Capabilities**: new methods or normalized types; which backends can provide them;
     what skips where.
   - **Backend work**: per backend, the commands or API calls, parsers, snapshot impact.
   - **Tests and timing**: unit tests per layer, lab tests and their steps, `timing.yaml`
     checks with SLA and timeout.
   - **Failure report**: the verdict line and the discriminating evidence (ADR-0005).
   - **Edge cases**: degraded lab, device not answering, restore after the test, faults.
5. **Write the design** to `docs/plans/YYYY-MM-DD-<name>-design.md`:
   Summary, Approach (and rejected ones), Intent and expected state, Capabilities, Backend
   work, Tests and timing, Failure report, Edge cases, ADR impact, Open questions.

Next step after agreement: `requirements-planner` for a requirements document, or `/plan`
for the implementation plan.
