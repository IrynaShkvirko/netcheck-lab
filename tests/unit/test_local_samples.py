"""The local recorder (`make record-fixtures`): what it calls on each device, what it
writes, and that the samples replay through the same adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from netcheck.backends import provenance
from netcheck.backends.local import LocalBackend, LocalFactory
from netcheck.devices.assembly import DeviceSpec, assemble
from netcheck.devices.transport.base import CommandResult, CommandTransport
from netcheck.model.expected import Expected
from netcheck.model.intent import Intent, load_intent
from netcheck.run.local_samples import (
    NotConverged,
    RecordingTransport,
    exercise,
    record_local_samples,
)

from .fakes import fast_timing
from .fakes_local import ScriptedTransport
from .intents import ROOT

EXPECTED = Expected(load_intent(ROOT / "intent" / "lab.yaml"))

CANNED: dict[str, Any] = {
    "ip -j link show": [
        {"ifname": f"eth{i}", "flags": ["UP"], "operstate": "UP"} for i in range(1, 5)
    ],
    "vtysh -c 'show ip ospf neighbor json'": {"neighbors": {}},
    "vtysh -c 'show ip route ospf json'": {},
    "vtysh -c 'show ip ospf route json'": {},
    "vtysh -c 'show running-config'": "hostname r1\n",
    "bridge -j vlan show": [],
    "ip -j route show": [],
    "ip -j neigh show": [],
}


def converged_ospf(router: str, state: str = "Full/-") -> dict[str, Any]:
    """FRR's neighbor and route JSON for `router` on the intended lab."""
    neighbors: dict[str, list[dict[str, str]]] = {}
    for a in EXPECTED.adjacencies():
        if a.router == router:
            iface = f"eth{a.local_port[1:]}:10.0.0.0"
            neighbors.setdefault(a.router_id, []).append({"nbrState": state, "ifaceName": iface})
    routes = {
        p: [
            {
                "protocol": "ospf",
                "selected": True,
                "installed": True,
                "nexthops": [
                    {"ip": h.address, "active": True} for h in EXPECTED.ospf_next_hops(router, p)
                ],
            }
        ]
        for p in EXPECTED.routes(router)
    }
    return {
        "vtysh -c 'show ip ospf neighbor json'": {"neighbors": neighbors},
        "vtysh -c 'show ip route ospf json'": routes,
    }


class CannedTransport:
    """Answers the read commands above (routers: converged OSPF, or adjacencies stuck in
    `state`), and any ping with three replies."""

    def __init__(self, device: str, state: str = "Full/-") -> None:
        ospf = converged_ospf(device, state) if device in EXPECTED.routers() else {}
        self.replies = {**CANNED, **ospf}

    def run(self, cmd: str, *, stdin: str | None = None, timeout: float = 30.0) -> CommandResult:
        if cmd.startswith("ping "):
            return CommandResult(cmd, "3 packets transmitted, 3 received, 0% packet loss\n", "", 0)
        reply = self.replies.get(cmd, "")
        return CommandResult(cmd, reply if isinstance(reply, str) else json.dumps(reply), "", 0)

    def copy_out(self, src: str, dest: str) -> None:
        pass


@pytest.fixture
def backend(intent: Intent, tmp_path: Path, monkeypatch) -> LocalBackend:  # type: ignore[no-untyped-def]
    settings = yaml.safe_load((ROOT / "backends.yaml").read_text())["local"]
    b = LocalBackend(intent, settings, tmp_path)
    monkeypatch.setattr(b, "open_transport", lambda spec: CannedTransport(spec.name))
    monkeypatch.setattr(b, "reset", lambda snapshot_id=None: "baseline")
    monkeypatch.setattr(
        b, "environment", lambda: {"nos": "FRRouting 10.1_git (r1)", "containerlab": "0.76.0"}
    )
    return b


def test_recording_transport_passes_calls_through_and_keeps_them() -> None:
    inner = ScriptedTransport({"ip -j link show": ["[]"]})
    rec = RecordingTransport(inner)
    assert rec.run("ip -j link show").stdout == "[]"
    rec.run("ip link set eth1 down")
    assert rec.calls == [
        {"cmd": "ip -j link show", "rc": 0, "stdout": "[]", "stderr": ""},
        {"cmd": "ip link set eth1 down", "rc": 0, "stdout": "", "stderr": ""},
    ]
    assert inner.writes == ["ip link set eth1 down"]


def test_recorder_writes_one_sample_per_device_and_marks_it_captured(
    backend: LocalBackend, tmp_path: Path
) -> None:
    dest = tmp_path / "local"
    written = record_local_samples(backend, dest, fast_timing())
    assert sorted(p.name for p in written) == sorted(f"{n}.json" for n in backend.intent.nodes)
    r1 = json.loads((dest / "r1.json").read_text())
    assert (r1["device"], r1["role"], r1["intent"]) == ("r1", "router", "campus-mini")
    assert {c["cmd"] for c in r1["calls"]} >= {
        "vtysh -c 'show ip ospf neighbor json'",
        "vtysh -c 'show ip route ospf json'",
        "vtysh -c 'show ip ospf route json'",
        "vtysh -c 'show running-config'",
        "ip -j link show",
    }
    h1 = [c["cmd"] for c in json.loads((dest / "h1.json").read_text())["calls"]]
    pings = [c.split()[-1] for c in h1 if c.startswith("ping ")]
    assert pings == ["10.0.10.12", "10.0.20.13", "10.0.30.14"]  # h2, h3 (isolated), h4
    status = provenance.status(dest)
    assert (status.captured, status.total) == (7, 7)
    assert "FRRouting 10.1_git" in provenance.load(dest)[0].nos


def test_recorder_refuses_a_lab_that_has_not_converged(
    backend: LocalBackend, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Right after a deploy the adjacencies are still Loading: recording then would turn a
    converging lab into the reference samples. Nothing is written."""
    monkeypatch.setattr(
        backend, "open_transport", lambda spec: CannedTransport(spec.name, "Loading/-")
    )
    dest = tmp_path / "local"
    with pytest.raises(NotConverged) as exc:
        record_local_samples(backend, dest, fast_timing())
    assert "r1: adjacency 2.2.2.2 via p2 not Full" in str(exc.value)
    assert not dest.exists()


def test_samples_replay_through_the_same_adapters(backend: LocalBackend, tmp_path: Path) -> None:
    """The contract tests' premise: replaying a device's recorded calls answers every
    command the adapters send, so nothing falls through to a write."""
    dest = tmp_path / "local"
    record_local_samples(backend, dest, fast_timing())
    transports: dict[str, ScriptedTransport] = {}

    def replay(spec: DeviceSpec) -> CommandTransport:
        calls = json.loads((dest / f"{spec.name}.json").read_text())["calls"]
        replies: dict[str, list[Any]] = {}
        for c in calls:
            replies.setdefault(c["cmd"], []).append(
                CommandResult(c["cmd"], c["stdout"], c["stderr"], c["rc"])
            )
        transports[spec.name] = ScriptedTransport(replies)
        return transports[spec.name]

    devices = assemble(backend.intent, "local", "eth", LocalFactory(replay))
    exercise(devices, Expected(backend.intent))
    assert {n: t.writes for n, t in transports.items() if t.writes} == {}
