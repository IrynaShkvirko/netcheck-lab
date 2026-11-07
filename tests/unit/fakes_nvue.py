"""A fake NVUE REST API that serves the JSON samples in fixtures/nvue/ and records
every config change, so NVIDIA-backend code runs without a DSX Air simulation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from netcheck.backends.nvidia_air import PATHS, NvueBridge, NvueLinks, NvueOspf
from netcheck.capabilities import Bridge, Links, Ospf
from netcheck.device import Device
from netcheck.devicelog import DeviceLog
from netcheck.intent import Intent
from netcheck.lab import Lab
from netcheck.transport.base import ApiResult

SAMPLES = Path(__file__).parent / "fixtures" / "nvue"
_NAMES = {path: name for name, path in PATHS.items()}


class FakeNvueTransport:
    """GETs are answered from <SAMPLES>/<device>/<name>.<rev>.json (404 if missing).
    The revision flow (POST revision, PATCH/DELETE ?rev=, apply, GET revision/<id>)
    is accepted and recorded in `changes`; `apply_state` is what the apply ends in."""

    def __init__(self, device: str) -> None:
        self.dir = SAMPLES / device
        self.calls: list[tuple[str, str, Any, dict[str, str]]] = []
        self.revisions = 0
        self.apply_state = "applied"
        self.status_for: dict[tuple[str, str], int] = {}  # force an HTTP status

    @property
    def changes(self) -> list[tuple[str, str, Any]]:
        return [(m, p, b) for m, p, b, prm in self.calls if "rev" in prm and m != "GET"]

    def request(
        self, method: str, path: str, body: Any = None, params: dict[str, str] | None = None
    ) -> ApiResult:
        params = dict(params or {})
        self.calls.append((method, path, body, params))
        forced = self.status_for.get((method, path))
        if forced is not None:
            return ApiResult(method, path, forced, {"title": "forced"})
        if method == "POST" and path == "revision":
            self.revisions += 1
            return ApiResult(method, path, 200, {str(self.revisions): {"state": "pending"}})
        if method == "GET" and path.startswith("revision/"):
            return ApiResult(method, path, 200, {"state": self.apply_state})
        if method != "GET":
            return ApiResult(method, path, 200, {})
        return self._get(path, params.get("rev", "operational"))

    def _get(self, path: str, rev: str) -> ApiResult:
        sub = None
        if path.startswith("interface/"):  # one interface: a subtree of the full list
            path, sub = "interface", path.split("/", 1)[1]
        f = self.dir / f"{_NAMES.get(path, path)}.{rev}.json"
        if not f.exists():
            return ApiResult("GET", path, 404, {"title": f"no sample {f.name}"})
        body = json.loads(f.read_text())
        if sub is not None:
            if sub not in body:
                return ApiResult("GET", path, 404, {"title": f"no interface {sub}"})
            body = body[sub]
        return ApiResult("GET", path, 200, body)


def nvue_ports(intent: Intent, node: str) -> dict[str, str]:
    return {
        ep.port: f"swp{ep.port[1:]}"
        for lk in intent.links_of(node)
        for ep in (lk.a, lk.b)
        if ep.node == node
    }


def use_nvue_devices(lab: Lab) -> dict[str, FakeNvueTransport]:
    """Swap the fake routers and switches of a fake lab for NVUE capabilities over
    FakeNvueTransport (hosts stay fake shells). Returns the transports by device."""
    transports = {}
    for name, node in lab.intent.nodes.items():
        if node.role == "host":
            continue
        t = FakeNvueTransport(name)
        ports = nvue_ports(lab.intent, name)
        caps: dict[type, object] = {Links: NvueLinks(t, ports)}  # type: ignore[arg-type]
        if node.role == "router":
            caps[Ospf] = NvueOspf(t, ports)  # type: ignore[arg-type]
        else:
            caps[Bridge] = NvueBridge(t, ports)  # type: ignore[arg-type]
        lab.devices[name] = Device(name, node.role, "nvidia", DeviceLog(name), caps, ports)
        transports[name] = t
    return transports
