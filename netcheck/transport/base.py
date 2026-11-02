"""Transport protocols (ADR-0002): one for commands, one for APIs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from netcheck.devicelog import DeviceLog


class TransportError(RuntimeError):
    """The device could not be reached or returned an error."""


@dataclass
class CommandResult:
    cmd: str
    stdout: str
    stderr: str
    rc: int

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def json(self) -> Any:
        try:
            return json.loads(self.stdout or "null")
        except json.JSONDecodeError as exc:
            raise TransportError(f"expected JSON from {self.cmd!r}: {exc}") from exc

    def check(self) -> CommandResult:
        if not self.ok:
            raise TransportError(f"{self.cmd!r} failed rc={self.rc}: {self.stderr.strip()}")
        return self


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
    log: DeviceLog

    def run(
        self, cmd: str, *, stdin: str | None = None, timeout: float = 30.0
    ) -> CommandResult: ...


class ApiTransport(Protocol):
    log: DeviceLog

    def request(
        self, method: str, path: str, body: Any = None, params: dict[str, str] | None = None
    ) -> ApiResult: ...
