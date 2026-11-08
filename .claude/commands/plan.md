---
description: Write a netcheck implementation plan from a requirements document or design
argument-hint: [path to docs/requirements/... or docs/plans/...-design.md]
---

Follow `.claude/skills/writing-plans/SKILL.md` to write an implementation plan for:
$ARGUMENTS

If no document is given, ask for it (or offer the `requirements-planner` agent first).
Read the requirements, `GLOSSARY.md`, the ADRs of the area and the code the plan changes
before writing. Put unknowns about external systems into phase 0 spikes. Write to
`docs/plans/YYYY-MM-DD-<feature>-implementation-plan.md` and end by listing which REQ ids
each task covers.
