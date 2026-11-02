"""Local backend: FRR routers, Linux-bridge switch and Linux hosts in Containerlab."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from netcheck.backends.base import Backend
from netcheck.capabilities import (
    Bridge,
    CaptureHandle,
    Links,
    Ospf,
    OspfNeighbor,
    PingResult,
    PortVlans,
    Route,
    Shell,
)
from netcheck.device import Device
from netcheck.devicelog import DeviceLog
from netcheck.render import port_name, render
from netcheck.transport.base import CommandResult, TransportError
from netcheck.transport.docker_exec import DockerExecTransport

_PING = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")


def parse_ping(src: str, dst: str, output: str) -> PingResult:
    m = _PING.search(output)
    if not m:
        return PingResult(src, dst, 0, 0, output)
    return PingResult(src, dst, int(m.group(1)), int(m.group(2)), output)


def _state(raw: str) -> str:
    """FRR reports 'Full/-' or 'Full/DR'; keep the adjacency part."""
    return raw.split("/")[0] if raw else "unknown"


def parse_frr_neighbors(data: dict[str, Any], to_logical: dict[str, str]) -> list[OspfNeighbor]:
    out = []
    for rid, entries in (data.get("neighbors") or {}).items():
        for e in entries:
            state = e.get("nbrState") or e.get("state") or e.get("converged") or ""
            iface = str(e.get("ifaceName") or e.get("interfaceName") or "").split(":")[0]
            out.append(OspfNeighbor(rid, _state(state), to_logical.get(iface, iface)))
    return out


def parse_frr_routes(data: dict[str, Any]) -> dict[str, Route]:
    out = {}
    for prefix, entries in (data or {}).items():
        for e in entries:
            if e.get("selected") or e.get("installed"):
                hops = tuple(
                    nh["ip"]
                    for nh in e.get("nexthops", [])
                    if nh.get("ip") and nh.get("active", True)
                )
                out[prefix] = Route(prefix, e.get("protocol", "?"), hops, bool(e.get("installed")))
    return out


def parse_bridge_vlans(
    data: list[dict[str, Any]], to_logical: dict[str, str]
) -> dict[str, PortVlans]:
    out = {}
    for entry in data or []:
        ifname = entry.get("ifname", "")
        if ifname not in to_logical:
            continue  # br0 itself and unmapped ports
        vlans = entry.get("vlans", [])
        vids = tuple(sorted(int(v["vlan"]) for v in vlans))
        access = (
            len(vlans) == 1
            and "PVID" in vlans[0].get("flags", [])
            and "Egress Untagged" in vlans[0].get("flags", [])
        )
        out[to_logical[ifname]] = PortVlans("access" if access else "trunk", vids)
    return dict(sorted(out.items()))


# ---- capabilities --------------------------------------------------------


class _Base:
    def __init__(self, transport: DockerExecTransport, port_map: dict[str, str]) -> None:
        self.t = transport
        self.port_map = port_map
        self.to_logical = {v: k for k, v in port_map.items()}

    def real(self, logical: str) -> str:
        return self.port_map[logical]

    def run(self, cmd: str, **kw: Any) -> CommandResult:
        return self.t.run(cmd, **kw)


class FrrOspf(_Base, Ospf):
    def neighbors(self) -> list[OspfNeighbor]:
        data = self.run("vtysh -c 'show ip ospf neighbor json'").check().json()
        return parse_frr_neighbors(data, self.to_logical)

    def routes(self) -> dict[str, Route]:
        return parse_frr_routes(self.run("vtysh -c 'show ip route ospf json'").check().json())

    def lsdb_prefixes(self) -> set[str]:
        """Prefixes OSPF knows about (its SPF result, derived from the LSDB)."""
        data = self.run("vtysh -c 'show ip ospf route json'").check().json() or {}
        prefixes = set()
        for key in data:
            try:
                prefixes.add(str(ipaddress.ip_network(key)))
            except ValueError:
                continue  # router entries, not networks
        return prefixes

    def running_config(self) -> str:
        return self.run("vtysh -c 'show running-config'").check().stdout


class LinuxBridge(_Base, Bridge):
    def vlan_membership(self) -> dict[str, PortVlans]:
        return parse_bridge_vlans(self.run("bridge -j vlan show").check().json(), self.to_logical)

    def set_access_vlan(self, port: str, vlan: int) -> None:
        ifname = self.real(port)
        current = self.vlan_membership().get(port)
        for vid in current.vlans if current else ():
            self.run(f"bridge vlan del dev {ifname} vid {vid}").check()
        self.run(f"bridge vlan add dev {ifname} vid {vlan} pvid untagged").check()


class LinuxLinks(_Base, Links):
    def set_link(self, port: str, up: bool) -> None:
        self.run(f"ip link set {self.real(port)} {'up' if up else 'down'}").check()

    def link_state(self, port: str) -> dict[str, str]:
        (info,) = self.run(f"ip -j link show {self.real(port)}").check().json()
        return {
            "port": port,
            "ifname": info["ifname"],
            "operstate": info.get("operstate", "?"),
            "admin": "up" if "UP" in info.get("flags", []) else "down",
        }


class LinuxShell(_Base, Shell):
    def __init__(
        self, transport: DockerExecTransport, port_map: dict[str, str], device: str
    ) -> None:
        super().__init__(transport, port_map)
        self.device = device

    def ping(
        self,
        dst: str,
        *,
        count: int = 3,
        interval: float = 0.2,
        deadline: float = 4.0,
        src_port: str = "p1",
    ) -> PingResult:
        res = self.run(
            f"ping -n -c {count} -i {interval} -W 1 -w {int(deadline)} {dst}", timeout=deadline + 10
        )
        return parse_ping(self.device, dst, res.stdout)

    def add_host_route(self, prefix: str, port: str = "p1") -> None:
        self.run(f"ip route replace {prefix} dev {self.real(port)} scope link").check()

    def del_host_route(self, prefix: str, port: str = "p1") -> None:
        self.run(f"ip route del {prefix} dev {self.real(port)}")

    def flush_neighbors(self, port: str = "p1") -> None:
        self.run(f"ip neigh flush dev {self.real(port)}")

    def neighbors(self) -> list[dict[str, str]]:
        return list(self.run("ip -j neigh show").check().json() or [])

    def start_capture(self, port: str, bpf: str = "") -> CaptureHandle:
        path = f"/tmp/netcheck-{secrets.token_hex(3)}.pcap"
        res = self.run(
            f"tcpdump -i {self.real(port)} -nn -U -w {path} {shlex.quote(bpf)} "
            f">/dev/null 2>&1 & echo $!"
        ).check()
        time.sleep(0.5)  # let tcpdump attach before traffic starts
        return CaptureHandle(self.device, port, path, res.stdout.strip())

    def stop_capture(self, handle: CaptureHandle, dest_dir: str) -> CaptureHandle:
        if handle.stopped:
            return handle
        handle.stopped = True
        self.run(f"kill -INT {handle.pid} 2>/dev/null; sleep 0.3")
        handle.summary = self.run(
            f"tcpdump -nn -r {handle.remote_path} -c 20 2>/dev/null"
        ).stdout.splitlines()
        dest = Path(dest_dir) / f"{handle.device}-{handle.port}.pcap"
        try:
            self.t.copy_out(handle.remote_path, str(dest))
            handle.local_path = str(dest)
        except subprocess.CalledProcessError:
            handle.local_path = ""
        return handle

    def start_stream(self, dst: str, interval: float, deadline: float) -> str:
        path = f"/tmp/netcheck-stream-{secrets.token_hex(3)}.txt"
        res = self.run(
            f"ping -n -q -i {interval} -w {int(deadline)} {dst} >{path} 2>&1 & echo $!"
        ).check()
        return f"{res.stdout.strip()}:{path}"

    def stop_stream(self, token: str) -> PingResult:
        pid, path = token.split(":", 1)
        self.run(f"kill -INT {pid} 2>/dev/null; sleep 0.3")
        out = self.run(f"cat {path}").stdout
        return parse_ping(self.device, "stream", out)


# ---- backend -------------------------------------------------------------


class LocalBackend(Backend):
    name = "local"
    timing_profile = "local"

    def __init__(self, intent: Any, settings: dict[str, Any], root: Path) -> None:
        super().__init__(intent, settings, root)
        self.prefix = settings.get("port_prefix", "eth")
        self.build_dir = root / "build" / intent.name
        self.clab = shlex.split(os.environ.get("NETCHECK_CLAB_CMD", "sudo -E containerlab"))
        self._devices: dict[str, Device] | None = None

    # lifecycle
    @property
    def topology_file(self) -> Path:
        return self.build_dir / f"{self.intent.name}.clab.yml"

    def container(self, node: str) -> str:
        return f"clab-{self.intent.name}-{node}"

    def is_running(self) -> bool:
        out = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"label=containerlab={self.intent.name}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        return len(out) == len(self.intent.nodes)

    def deploy(self, fresh: bool = False) -> bool:
        render(self.intent, self.settings["images"], self.build_dir, self.prefix)
        if self.is_running() and not fresh:
            return False
        subprocess.run(
            [*self.clab, "deploy", "-t", str(self.topology_file), "--reconfigure"], check=True
        )
        return True

    def destroy(self) -> None:
        subprocess.run(
            [*self.clab, "destroy", "-t", str(self.topology_file), "--cleanup"], check=False
        )

    # devices
    def devices(self) -> dict[str, Device]:
        if self._devices is None:
            self._devices = {
                name: self._build(name, node.role) for name, node in self.intent.nodes.items()
            }
        return self._devices

    def _build(self, name: str, role: str) -> Device:
        log = DeviceLog(name)
        t = DockerExecTransport(self.container(name), log)
        ports = {
            ep.port: port_name(ep.port, self.prefix)
            for lk in self.intent.links_of(name)
            for ep in (lk.a, lk.b)
            if ep.node == name
        }
        caps: dict[type, object] = {Links: LinuxLinks(t, ports)}
        if role == "router":
            caps[Ospf] = FrrOspf(t, ports)
        if role == "switch":
            caps[Bridge] = LinuxBridge(t, ports)
        if role in ("host", "switch"):
            caps[Shell] = LinuxShell(t, ports, name)
        return Device(
            name=name, role=role, backend=self.name, log=log, capabilities=caps, port_map=ports
        )

    # snapshots (ADR-0003)
    def snapshot(self, label: str) -> str:
        snap_id = f"{label}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        data: dict[str, Any] = {
            "id": snap_id,
            "intent": self.intent.digest,
            "frr": {},
            "bridge": {},
            "links": {},
        }
        for name, dev in self.devices().items():
            data["links"][name] = {p: dev.links.link_state(p)["admin"] for p in dev.port_map}
            if dev.has(Ospf):
                data["frr"][name] = dev.ospf.running_config()
            if dev.has(Bridge):
                data["bridge"][name] = {
                    p: [pv.mode, list(pv.vlans)] for p, pv in dev.bridge.vlan_membership().items()
                }
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        (self.snapshot_dir / f"{snap_id}.json").write_text(json.dumps(data, indent=2))
        return snap_id

    def restore(self, snapshot_id: str) -> None:
        data = json.loads((self.snapshot_dir / f"{snapshot_id}.json").read_text())
        devs = self.devices()
        for name, ports in data["links"].items():
            for port, admin in ports.items():
                if devs[name].links.link_state(port)["admin"] != admin:
                    devs[name].links.set_link(port, admin == "up")
        for name, ports in data["bridge"].items():
            live = devs[name].bridge.vlan_membership()
            for port, (mode, vlans) in ports.items():
                cur = live.get(port)
                if cur and cur.mode == mode and list(cur.vlans) == vlans:
                    continue
                self._restore_port(devs[name], port, mode, vlans)
        for name, config in data["frr"].items():
            t = devs[name].ospf  # FrrOspf
            assert isinstance(t, FrrOspf)
            t.run("cat > /tmp/netcheck-restore.conf", stdin=config).check()
            res = t.run(
                "/usr/lib/frr/frr-reload.py --reload /tmp/netcheck-restore.conf", timeout=60
            )
            if not res.ok:
                raise TransportError(f"{name}: frr-reload failed: {res.stderr.strip()[-300:]}")

    @staticmethod
    def _restore_port(dev: Device, port: str, mode: str, vlans: list[int]) -> None:
        br = dev.bridge
        assert isinstance(br, LinuxBridge)
        ifname = br.real(port)
        cur = br.vlan_membership().get(port)
        for vid in cur.vlans if cur else ():
            br.run(f"bridge vlan del dev {ifname} vid {vid}").check()
        for vid in vlans:
            extra = " pvid untagged" if mode == "access" else ""
            br.run(f"bridge vlan add dev {ifname} vid {vid}{extra}").check()

    def environment(self) -> dict[str, str]:
        env = {
            "topology": self.intent.name,
            "backend": self.name,
            "intent": self.intent.digest,
            "containerlab": str(self.settings.get("containerlab_version", "?")),
        }
        env.update({f"image.{k}": v for k, v in self.settings["images"].items()})
        try:
            routers = [d for d in self.devices().values() if d.has(Ospf)]
            if routers:
                ospf = routers[0].ospf
                assert isinstance(ospf, FrrOspf)
                first = ospf.run("vtysh -c 'show version'").stdout.splitlines()
                env["nos"] = first[0].strip() if first else "FRR ?"
        except TransportError:
            env["nos"] = "unreachable"
        return env
