"""A scripted command transport, so local-backend adapters run without containers.

The counterpart of fakes_nvue.FakeNvueTransport for the CommandTransport seam.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from netcheck.devices.transport.base import CommandResult

# A reply is stdout (str), JSON-able data (dumped as stdout), or a full CommandResult
# (to give an exit code and stderr).
Reply = str | list[Any] | dict[str, Any] | CommandResult


class ScriptedTransport:
    """Each command gets its replies in order (the last one repeats). A command with no
    reply is a change to the device: it succeeds and is recorded in `writes`, unless a
    `default` reply is given, which then answers every unscripted command."""

    def __init__(
        self, replies: Mapping[str, Sequence[Reply]] | None = None, default: Reply | None = None
    ) -> None:
        self.replies: dict[str, list[Reply]] = {c: list(r) for c, r in (replies or {}).items()}
        self.default = default
        self.writes: list[str] = []
        self.commands: list[str] = []  # every command, in order
        self.copies: list[tuple[str, str]] = []

    def run(self, cmd: str, *, stdin: str | None = None, timeout: float = 30.0) -> CommandResult:
        self.commands.append(cmd)
        if cmd in self.replies:
            queue = self.replies[cmd]
            return _result(cmd, queue.pop(0) if len(queue) > 1 else queue[0])
        if self.default is not None:
            return _result(cmd, self.default)
        self.writes.append(cmd)
        return CommandResult(cmd, "", "", 0)

    def copy_out(self, src: str, dest: str) -> None:
        self.copies.append((src, dest))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_text(f"copied from {src}\n")


def _result(cmd: str, reply: Reply) -> CommandResult:
    if isinstance(reply, CommandResult):
        return CommandResult(cmd, reply.stdout, reply.stderr, reply.rc, reply.target)
    out = reply if isinstance(reply, str) else json.dumps(reply)
    return CommandResult(cmd, out, "", 0)
