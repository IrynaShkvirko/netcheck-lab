"""Command transport over `docker exec` (local backend)."""

from __future__ import annotations

import subprocess
import time

from netcheck.devicelog import DeviceLog
from netcheck.transport.base import CommandResult, TransportError


class DockerExecTransport:
    def __init__(self, container: str, log: DeviceLog) -> None:
        self.container = container
        self.log = log

    def run(self, cmd: str, *, stdin: str | None = None, timeout: float = 30.0) -> CommandResult:
        started = time.time()
        argv = ["docker", "exec", "-i", self.container, "sh", "-c", cmd]
        try:
            proc = subprocess.run(
                argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            self.log.record("exec", cmd, "TIMEOUT", None, started)
            raise TransportError(f"{self.container}: {cmd!r} timed out after {timeout}s") from exc
        result = CommandResult(
            cmd=cmd,
            stdout=proc.stdout,
            stderr=proc.stderr,
            rc=proc.returncode,
            target=self.container,
        )
        self.log.record(
            "exec",
            cmd,
            proc.stdout if proc.returncode == 0 else proc.stderr,
            proc.returncode,
            started,
        )
        return result

    def copy_out(self, src: str, dest: str) -> None:
        subprocess.run(
            ["docker", "cp", f"{self.container}:{src}", dest], check=True, capture_output=True
        )
