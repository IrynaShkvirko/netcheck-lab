"""Record what the local lab's devices really answer, as unit-test samples.

`python -m netcheck record-fixtures` (`make record-fixtures`) restores the lab to its
baseline and waits until OSPF has converged (every intended adjacency Full, every intended
route installed: a lab just deployed is still converging). Then it drives every device
through the local backend's own adapters with a recording transport around `docker exec`:
link states, OSPF neighbors, routes, LSDB and running config, bridge VLANs, host routes,
and pings to each host's reachable and isolated peers. Each device's calls (command, exit
code, stdout, stderr) go to tests/unit/fixtures/local/<device>.json, marked captured in
provenance.yaml with the FRR version and images.

Because the samples are the adapters' own commands, the contract tests replay them through
the same adapters: a parser that misreads FRR or iproute2 output fails there, and so does
an adapter whose command changed without new samples.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from netcheck import provenance
from netcheck.assembly import DeviceSpec, assemble
from netcheck.backends.local import LinuxShell, LocalBackend, LocalFactory
from netcheck.capabilities import Bridge, Links, Ospf
from netcheck.expected import Expected
from netcheck.timing import Timing, WaitTimeout
from netcheck.transport.base import CommandResult, CommandTransport


class NotConverged(RuntimeError):
    """The lab did not reach the intended state in time; nothing was recorded."""


def ping(shell: LinuxShell, dst: str) -> Any:
    """The ping recorded for each host and peer; the contract tests send the same one."""
    return shell.ping(dst, count=3, interval=0.2, deadline=3.0)


@dataclass
class RecordingTransport:
    """Passes every call to `inner` and keeps what came back."""

    inner: CommandTransport
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(self, cmd: str, *, stdin: str | None = None, timeout: float = 30.0) -> CommandResult:
        result = self.inner.run(cmd, stdin=stdin, timeout=timeout)
        self.calls.append(
            {"cmd": cmd, "rc": result.rc, "stdout": result.stdout, "stderr": result.stderr}
        )
        return result

    def copy_out(self, src: str, dest: str) -> None:
        self.inner.copy_out(src, dest)


def peers(host: str, pairs: list[tuple[str, str]]) -> list[str]:
    """The hosts paired with `host` in `pairs`, in either order."""
    return sorted({b if a == host else a for a, b in pairs if host in (a, b)})


def exercise(devices: dict[str, Any], expected: Expected) -> None:
    """Call every read the lab tests make on a healthy lab, device by device."""
    intent = expected.intent
    for dev in devices.values():
        if dev.has(Links):
            dev.links.link_states(list(dev.port_map))
        if dev.has(Ospf):
            dev.ospf.neighbors()
            dev.ospf.routes()
            dev.ospf.lsdb_prefixes()
            dev.ospf.interface_settings()
        if dev.has(Bridge):
            dev.bridge.vlan_membership()
        if dev.role == "host":
            shell = cast(LinuxShell, dev.shell)
            shell.route_table()
            for peer in peers(dev.name, expected.reachable_pairs() + expected.isolated_pairs()):
                ping(shell, intent.host_ip(peer))
            shell.neighbors()


def ospf_gaps(devices: dict[str, Any], expected: Expected) -> list[str]:
    """What still differs from converged OSPF: adjacencies not Full, routes not installed."""
    gaps = []
    for router in expected.routers():
        ospf = devices[router].ospf
        full = sorted((n.router_id, n.port) for n in ospf.neighbors() if n.state == "Full")
        want = sorted(
            (a.router_id, a.local_port) for a in expected.adjacencies() if a.router == router
        )
        gaps += [
            f"{router}: adjacency {rid} via {port} not Full"
            for rid, port in want
            if (rid, port) not in full
        ]
        installed = {p for p, r in ospf.routes().items() if r.installed}
        gaps += [
            f"{router}: route {p} not installed"
            for p in sorted(expected.routes(router) - installed)
        ]
    return gaps


def wait_converged(devices: dict[str, Any], expected: Expected, timing: Timing) -> None:
    """Poll until ospf_gaps is empty, within the ospf_neighbor_full timeout; raise
    NotConverged with what was still missing."""
    try:
        timing.wait_until(
            "ospf_neighbor_full", lambda: ospf_gaps(devices, expected), lambda g: not g
        )
    except WaitTimeout as exc:
        missing = "; ".join(exc.last) if isinstance(exc.last, list) else exc.describe("unknown")
        raise NotConverged(f"OSPF has not converged, nothing recorded: {missing}") from exc


def record_local_samples(backend: LocalBackend, dest: Path, timing: Timing) -> list[Path]:
    """Restore the baseline, wait for OSPF to converge, record every device's answers into
    `dest` and mark them captured. Returns the files written."""
    backend.reset()  # a healthy lab: the samples must show the intended state
    expected = Expected(backend.intent)
    wait_converged(backend.devices(), expected, timing)
    recorders: dict[str, RecordingTransport] = {}

    def open_transport(spec: DeviceSpec) -> CommandTransport:
        recorders[spec.name] = RecordingTransport(backend.open_transport(spec))
        return recorders[spec.name]

    factory = LocalFactory(open_transport)
    devices = assemble(backend.intent, backend.name, backend.prefix, factory)
    exercise(devices, expected)

    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name, dev in devices.items():
        path = dest / f"{name}.json"
        sample = {
            "device": name,
            "role": dev.role,
            "intent": backend.intent.name,
            "calls": recorders[name].calls if name in recorders else [],
        }
        path.write_text(json.dumps(sample, indent=2) + "\n")
        written.append(path)
    env = backend.environment()
    images = ", ".join(f"{k} {v}" for k, v in sorted(backend.settings["images"].items()))
    nos = f"{env.get('nos', 'FRR ?')}; containerlab {env.get('containerlab', '?')}; {images}"
    provenance.mark_captured(dest, written, nos)
    return written
