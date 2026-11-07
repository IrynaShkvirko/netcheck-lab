"""API transport for the NVIDIA Cumulus NVUE REST API (NVIDIA backend)."""

from __future__ import annotations

import time
from typing import Any

import requests

from netcheck.devicelog import DeviceLog
from netcheck.transport.base import ApiResult, TransportError

SECRET_WORDS = ("password", "secret", "token", "key", "community", "credential")


def redact(value: Any) -> Any:
    """Copy of an API body with values of secret-looking keys replaced, for logs that
    end up in CI artifacts."""
    if isinstance(value, dict):
        return {
            k: "***" if any(w in str(k).lower() for w in SECRET_WORDS) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class NvueRestTransport:
    """Thin wrapper over https://<host>:8765/nvue_v1/...

    Config changes follow the NVUE revision flow: create a revision, PATCH
    changes into it, then apply it (see apply_changes in backends/nvidia_air.py).
    """

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        log: DeviceLog,
        port: int = 8765,
        verify_tls: bool | str = True,
        timeout: float = 15.0,
    ) -> None:
        self.base = f"https://{host}:{port}/nvue_v1"
        self.log = log
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (user, password)
        # True: system CAs; a path: that CA bundle; False: no check (self-signed lab only)
        self.session.verify = verify_tls

    def request(
        self, method: str, path: str, body: Any = None, params: dict[str, str] | None = None
    ) -> ApiResult:
        started = time.time()
        url = f"{self.base}/{path.lstrip('/')}"
        try:
            resp = self.session.request(method, url, json=body, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            self.log.record("api", f"{method} {path}", str(exc), None, started)
            raise TransportError(f"{method} {url}: {exc}") from exc
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = resp.text
        self.log.record("api", f"{method} {path}", redact(payload), resp.status_code, started)
        return ApiResult(method=method, path=path, status=resp.status_code, body=payload)
