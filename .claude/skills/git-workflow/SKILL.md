---
name: git-workflow
description: "netcheck's git conventions: branch names, commit subjects, PR titles and bodies, the pre-push gate, issue links. Use when creating a branch, writing a commit message, opening a pull request, or any git operation that must follow this repo's style."
---

# Git workflow (netcheck)

## Commit and push only when asked

Commit after the user asks for a commit; push and open a PR after they ask for that. One
request, one commit (or one push). If a hook fails, fix the cause and wait for the next ask.

## Branches

- Work happens on a branch off `master`; `master` only changes through merged PRs.
- Name: short kebab-case of the change, no type prefix: `transport-seam`, `route-diagnosis`,
  `lab-mutation-baseline`. A multi-phase plan uses `<plan>/<phase>-<name>`
  (`nvidia-backend/1-air-client`).
- When an issue exists, its number may lead: `23-nvue-lsdb`.

## Commit subjects

The repo's style (see `git log --oneline`): one imperative sentence, capitalized, no
trailing period, no type prefix, under ~72 characters, saying what changes in the
domain's words.

- `Verify links after a snapshot restore`
- `Refuse to deploy during a lab mutation run (NETCHECK_NO_DEPLOY)`
- `Backend.up decides whether to reuse or redeploy the lab`

A body is optional: a short paragraph on why, when the subject cannot carry it. Reference
an issue as `Fixes #23` in the body. End with the attribution trailer the session asks for.

Split a change that does two things into two commits; a test belongs in the same commit as
the code it covers.

## Pre-push gate

```bash
make check          # ruff check + ruff format --check + mypy + tests/unit
```

Push only when it passes. When the change touches lab behaviour (a backend adapter, the
lifecycle, a lab test), also run `make test-lab` on a lab (the local one, or
`BACKEND=nvidia`) and say in the PR whether it ran.

## Pull requests

- Title: the same style as a commit subject; for a one-commit PR, the commit subject.
- Body: use the `/pr` skill (`mattpocock-skills:pr`). The repo's PRs have **Summary**,
  **Evidence** (commands run and their output, trimmed), and **Merge Danger** (door
  one-way/two-way, blast radius). Name the files that matter with backticks; link
  `docs/requirements/` and `docs/plans/` when the PR implements a plan task
  ("Plan: tasks A1, A2").
- Issues are GitHub Issues on `IrynaShkvirko/netcheck-lab` (`docs/agents/issue-tracker.md`);
  write `Fixes #N` to close one.
- After opening, bind the PR in the app and watch CI there; do not poll it from the shell.
