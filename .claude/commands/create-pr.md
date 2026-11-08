---
description: Push the branch and open a netcheck pull request with Summary, Evidence and Merge Danger
---

Follow `.claude/skills/git-workflow/SKILL.md`; write the body with the `/pr` skill
(`mattpocock-skills:pr`).

1. `git branch --show-current` (not `master`); `git log origin/master..HEAD --oneline`;
   `git diff origin/master...HEAD --stat`.
2. `make check` must pass. If the branch changes lab behaviour, say whether `make test-lab`
   ran and on which backend; never claim a lab run that did not happen.
3. Title: the repo's subject style (the commit subject for a one-commit PR).
4. Body: **Summary** (what and why, with the plan tasks or REQ ids it implements),
   **Evidence** (commands and trimmed output), **Merge Danger** (door, blast radius), then
   the attribution line the session asks for. Link `Fixes #N` when it closes an issue.
5. `git push -u origin HEAD`, then `gh pr create --base master --title ... --body-file ...`.
6. Report the PR URL; bind it in the app so CI is watched there.
