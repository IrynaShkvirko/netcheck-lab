---
description: Create a netcheck branch off the latest master
argument-hint: [short description, optional issue number or plan/phase]
---

Follow the branch rules in `.claude/skills/git-workflow/SKILL.md` for: $ARGUMENTS

1. `git status --short`: if the tree is dirty, ask whether to carry, stash or stop.
2. `git fetch origin` and branch from `origin/master`.
3. Name: kebab-case of the change, no type prefix; `<issue>-<name>` when an issue is given;
   `<plan>/<phase>-<name>` for a plan phase (e.g. `nvidia-backend/1-air-client`).
4. `git switch -c <name> origin/master`, then report the name and the base commit.
