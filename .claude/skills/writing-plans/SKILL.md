---
name: writing-plans
description: "Write a step-by-step implementation plan for netcheck from agreed requirements or a design: small verifiable tasks, exact files, parallel tracks, spikes for unknowns. Use when requirements or a design are approved and implementation has not started."
---

# Writing a netcheck implementation plan

Write for a capable engineer who has never seen netcheck: exact files, signatures, the
pattern to copy, and how to know each task is done. The NVIDIA backend plan
(`docs/plans/2026-11-08-nvidia-backend-implementation-plan.md`) is the reference example.

## Inputs

- A requirements document (`docs/requirements/<feature>-requirements.md`, REQ-x.y ids) or an
  agreed design (`docs/plans/*-design.md`).
- `GLOSSARY.md`, the ADRs of the area, and the current code. Read the code before writing a
  task that changes it; quote real names (`Backend.up`, `PortMap.real`, `apply_changes`).

## Structure

Write to `docs/plans/YYYY-MM-DD-<feature>-implementation-plan.md`:

1. Header: requirements link, date, branch naming per phase.
2. **Goal** (1–2 sentences) and **What exists, what is missing** (a table).
3. **Architecture**: a small diagram, and the design choices the plan commits to, each
   with the ADR it adds or changes.
4. **Files to create / modify**, one line of purpose each.
5. **Phase 0: spikes** for every fact only a real lab or external service can give
   (API shapes, boot times, image contents). Each spike has steps and a "Done when";
   tasks that depend on it mark the spot **[S#]** and keep it to one constant or function.
6. **Phases of tasks.** Each task:
   - `#### Task <id>: <imperative title>`
   - **Files:** exact paths
   - numbered steps with signatures or short code where the shape matters
   - **Verification:** checkboxes with a command and its expected result
7. **Parallel execution** table: tracks, tasks, files owned, what runs together, merge order.
8. **Risks** table (likelihood, impact, mitigation) and a **Completion checklist**.

## Task rules

- **Small:** one module or one behaviour per task; more than ~6 steps means split it.
- **Verifiable:** every task ends with `make check` green plus its own check
  (a named unit test passes, `python -m netcheck render --backend X` writes files, a grep finds
  nothing).
- **Ordered by the seam:** normalized types and capability interfaces -> parsers (pure, with
  samples) -> adapters over fake transports -> factory and assembly -> backend lifecycle ->
  CLI/Makefile -> live lab runs -> timing profile -> CI -> ADRs and README.
- **Tests in the same task** as the code they cover, named by behaviour, using the fakes in
  the `netcheck-testing` skill.
- **No forward references:** a task uses only what earlier tasks (or existing code) provide.
- **Live work last:** tasks needing a real lab or account are in their own phase, so
  everything before it is done and green offline.
- Every REQ maps to at least one task; say which in the task or in a coverage table.
- Unknowns are spikes or open questions, never guesses written as facts.
