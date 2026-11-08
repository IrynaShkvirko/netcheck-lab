---
description: Write the commit message for the staged change in netcheck's style and commit once
---

Follow `.claude/skills/git-workflow/SKILL.md`.

1. `git branch --show-current` (never commit on `master`), `git diff --staged --stat`,
   `git diff --staged`. If nothing is staged, show `git status --short` and ask what to stage.
2. If the staged change does two things, propose a split and stop.
3. Run `make check`; if it fails, report and stop.
4. Write one imperative subject in the repo's style (`git log --oneline -10` to match), an
   optional why-paragraph, `Fixes #N` when an issue is closed, and the attribution trailer
   the session asks for.
5. Commit once. If a hook fails, fix the cause and wait for the user to ask again.
