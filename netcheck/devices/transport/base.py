"""Transport protocols (ADR-0002): one for commands, one for APIs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


class TransportError(RuntimeError):
    """The device could not be reached or returned an error."""


@dataclass
class CommandResult:
    cmd: str
    stdout: str
    stderr: str
    rc: int
    target: str = ""  # device or container the command ran on

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def json(self) -> Any:
        try:
            return json.loads(self.stdout or "null")
        except json.JSONDecodeError as exc:
            raise TransportError(f"{self._where}expected JSON from {self.cmd!r}: {exc}") from exc

    def check(self) -> CommandResult:
        if not self.ok:
            raise TransportError(
                f"{self._where}{self.cmd!r} failed rc={self.rc}: {self.stderr.strip()}"
            )
        return self

    @property
    def _where(self) -> str:
        return f"{self.target}: " if self.target else ""


@dataclass
class ApiResult:
    method: str
    path: str
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class CommandTransport(Protocol):
    """Runs shell commands on a device (local backend: `docker exec`). Every real
    transport records each call in its device log (ADR-0002, ADR-0005)."""

    def run(
        self, cmd: str, *, stdin: str | None = None, timeout: float = 30.0
    ) -> CommandResult: ...

    def copy_out(self, src: str, dest: str) -> None:
        """Copy a file from the device to the local path `dest` (packet captures)."""
        ...


class ApiTransport(Protocol):
    """Calls a device's REST API (NVIDIA backend: NVUE). Real transports log every call."""

    def request(
        self, method: str, path: str, body: Any = None, params: dict[str, str] | None = None
    ) -> ApiResult: ...
