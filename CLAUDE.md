## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (`IrynaShkvirko/netcheck-lab`), via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical names, unchanged: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` at the repo root, ADRs in `docs/decisions/`. See `docs/agents/domain.md`.

### Mutation testing

mutmut over `tests/unit` (`make mutate`), configured in `pyproject.toml`. Run the control first and triage survivors by behaviour. See `.claude/skills/mutation-testing/SKILL.md`.
