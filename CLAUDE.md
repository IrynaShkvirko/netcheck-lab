## Spec-driven workflow

New features go idea -> requirements (`docs/requirements/`) -> implementation plan (`docs/plans/`) -> implementor -> reviewers -> plan-verifier. The agents, their order and the commands are in `.claude/agents/README.md`; project skills in `.claude/skills/README.md`. Current plan: NVIDIA backend (`docs/plans/2026-11-08-nvidia-backend-implementation-plan.md`).

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (`IrynaShkvirko/netcheck-lab`), via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical names, unchanged: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` at the repo root, ADRs in `docs/decisions/`. See `docs/agents/domain.md`.

### Mutation testing

mutmut over `tests/unit` (`make mutate`), configured in `pyproject.toml`; an opt-in lab profile over `tests/lab` (`make mutate-lab GROUP=...`, `mutation/lab/`) needs a running lab and never runs in CI. Run the control first and triage survivors by behaviour. Guarded modules have a floor and an accepted-survivor list in `pyproject.toml` (`make mutation-floor`, CI `mutation` job): kill a new survivor or accept it with a reason, never lower a floor. See `.claude/skills/mutation-testing/SKILL.md`.
