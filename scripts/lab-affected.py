#!/usr/bin/env python3
"""Decide whether a change needs the CI lab job: read changed paths on stdin, print
`lab=true` or `lab=false` (for $GITHUB_OUTPUT).

The lab is skipped only when every changed path is one the lab cannot be affected by: unit
tests, Markdown, docs, agent files, Dependabot config. Anything else, or no list at all (a
manual run, a first push), runs it: when in doubt, deploy.
"""

from __future__ import annotations

import re
import sys

NOT_LAB = re.compile(
    r"^(tests/unit/|docs/|\.claude/|mutation/)"
    r"|\.md$"
    r"|^\.github/dependabot\.yml$"
    r"|^\.gitignore$"
)


def needs_lab(paths: list[str]) -> bool:
    paths = [p.strip() for p in paths if p.strip()]
    return not paths or any(not NOT_LAB.search(p) for p in paths)


if __name__ == "__main__":
    print(f"lab={'true' if needs_lab(sys.stdin.read().splitlines()) else 'false'}")
