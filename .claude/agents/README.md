# Agents

Subagents for netcheck's spec-driven workflow. Each loads the skills in its `skills:`
frontmatter (from `.claude/skills/`) and is limited to the tools in `tools:`.

## The workflow

```
idea --/brainstorm--> design (docs/plans/*-design.md)
     --requirements-planner--> requirements (docs/requirements/*-requirements.md, REQ-x.y)
     --/plan--> implementation plan (docs/plans/*-implementation-plan.md: spikes, tasks, tracks)
     --implementor (one per track)--> code + unit tests, make check green
     --test-writer--> more tests where a task left gaps
     --backend-reviewer + security-reviewer--> findings fixed
     --plan-verifier--> COMPLETE / gaps
     --/commit, /create-pr--> PR
```

Refactoring takes the side road: `refactoring-planner` -> plan -> `implementor` per step.

The current example run: `docs/requirements/nvidia-backend-requirements.md` and
`docs/plans/2026-11-08-nvidia-backend-implementation-plan.md`.

## Catalog

| Agent | Writes | Use when |
| --- | --- | --- |
| [requirements-planner](requirements-planner.md) | requirements doc | starting a backend, NOS, capability, test area or report feature |
| [refactoring-planner](refactoring-planner.md) | refactoring plan | restructuring code without changing behaviour |
| [implementor](implementor.md) | code + tests | building a plan task or a scoped change |
| [test-writer](test-writer.md) | tests | code needs (more) unit or lab tests |
| [backend-reviewer](backend-reviewer.md) | findings | after changes to backends, transports, assembly, capabilities, config |
| [security-reviewer](security-reviewer.md) | findings | after changes touching credentials, connections, commands, logs, CI |
| [plan-verifier](plan-verifier.md) | coverage report | a plan or phase is claimed done |
| [verify-setup](verify-setup.md) | check report | after editing `.claude/` |

Parallel tracks in a plan own disjoint files, so one `implementor` per track can run at the
same time (in separate worktrees when they commit).
