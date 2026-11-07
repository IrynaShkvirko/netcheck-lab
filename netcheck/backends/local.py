"""Local backend: FRR routers, Linux-bridge switch and Linux hosts in Containerlab."""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from netcheck.assembly import CapabilityFactory, DeviceSpec, assemble
from netcheck.backends.base import Backend
from netcheck.capabilities import (
    Bridge,
    CaptureHandle,
    Links,
    LinkState,
    Ospf,
    OspfInterface,
    OspfNeighbor,
    PingResult,
    PortVlans,
    Route,
    Shell,
    Snapshot,
)
from netcheck.device import Device
from netcheck.ports import PortMap
from netcheck.render import BRIDGE, render
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


def parse_frr_neighbors(data: dict[str, Any], ports: PortMap) -> list[OspfNeighbor]:
    out = []
    for rid, entries in (data.get("neighbors") or {}).items():
        for e in entries:
            state = e.get("nbrState") or e.get("state") or e.get("converged") or ""
            iface = str(e.get("ifaceName") or e.get("interfaceName") or "").split(":")[0]
            out.append(OspfNeighbor(rid, _state(state), ports.logical(iface)))
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
    data: list[dict[str, Any]], ports: PortMap, bridges: tuple[str, ...] = (BRIDGE,)
) -> dict[str, PortVlans]:
    """`bridge -j vlan show` -> {port: PortVlans}.

    Ports the intent maps get their logical name (eth3 -> p3). A bridge port the intent
    does not know is kept under its real name (eth9), so drift checks can report it.
    The bridge device itself (br0) is not a port and is skipped.
    """
    out = {}
    for entry in data or []:
        ifname = entry.get("ifname", "")
        if not ifname or ifname in bridges:
            continue
        port = ports.logical(ifname)
        vlans = entry.get("vlans", [])
        vids = tuple(sorted(int(v["vlan"]) for v in vlans))
        if not vids:
            out[port] = PortVlans("none", ())
            continue
        access = (
            len(vlans) == 1
            and "PVID" in vlans[0].get("flags", [])
            and "Egress Untagged" in vlans[0].get("flags", [])
        )
        out[port] = PortVlans("access" if access else "trunk", vids)
    return dict(sorted(out.items()))


# ---- FRR config diff (used by snapshot restore) -----------------------------

_FRR_SKIP = ("Building configuration", "Current configuration", "frr version", "end")


def frr_sections(config: str) -> dict[str, list[str]]:
    """Split FRR running-config into {section header: [lines]}; "" holds global lines."""
    sections: dict[str, list[str]] = {"": []}
    current: str | None = None
    for raw in config.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped == "!" or stripped.startswith(_FRR_SKIP):
            if stripped == "!":
                current = None
            continue
        if stripped == "exit":
            current = None
        elif line.startswith(" ") and current is not None:
            sections[current].append(stripped)
        elif stripped.startswith(("interface ", "router ", "line ")):
            current = stripped
            sections.setdefault(current, [])
        else:
            current = None
            sections[""].append(stripped)
    return sections


def frr_config_diff(wanted: str, live: str) -> list[list[str]]:
    """vtysh command sequences (run under 'conf t') that turn `live` into `wanted`."""
    want, have = frr_sections(wanted), frr_sections(live)
    steps: list[list[str]] = []
    for section in sorted(set(want) | set(have)):
        w, h = want.get(section), have.get(section)
        if w is None:
            if section and not section.startswith("line "):
                steps.append([f"no {section}"])
            continue
        prefix = [section] if section else []
        extra = [f"no {ln}" for ln in (h or []) if ln not in w]
        missing = [ln for ln in w if ln not in (h or [])]
        if h is None or extra or missing:
            body = extra + missing
            if body or h is None:
                steps.append(prefix + body)
    return [s for s in steps if s]


def parse_frr_interfaces(config: str, ports: PortMap) -> dict[str, OspfInterface]:
    """OSPF settings per logical interface, from FRR running-config. The only place
    that reads FRR interface config text (assertions use the normalized result)."""
    out: dict[str, OspfInterface] = {}
    for section, lines in frr_sections(config).items():
        if not section.startswith("interface "):
            continue
        name = ports.logical(section.split()[1])
        fields: dict[str, Any] = {"addresses": []}
        for ln in lines:
            words = ln.split()
            if ln.startswith("ip address ") and len(words) >= 3:
                fields["addresses"].append(words[2])
            elif ln.startswith("ip ospf area ") and len(words) >= 4:
                fields["area"] = words[3]
            elif ln.startswith("ip ospf hello-interval ") and words[-1].isdigit():
                fields["hello_interval"] = int(words[-1])
            elif ln.startswith("ip ospf dead-interval ") and words[-1].isdigit():
                fields["dead_interval"] = int(words[-1])
            elif ln.startswith("ip ospf cost ") and words[-1].isdigit():
                fields["cost"] = int(words[-1])
            elif ln.startswith("ip ospf network ") and len(words) >= 4:
                fields["network"] = words[3]
            elif ln == "ip ospf passive":
                fields["passive"] = True
        fields["addresses"] = tuple(fields["addresses"])
        out[name] = OspfInterface(name=name, **fields)
    return out


def _route_key(r: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(r.get("dst", "")),
        str(r.get("gateway", "")),
        str(r.get("dev", "")),
        str(r.get("scope", "")),
    )


def host_route_diff(wanted: list[dict[str, Any]], live: list[dict[str, Any]]) -> list[str]:
    """`ip route` commands that turn the live host routes into the wanted ones.
    Connected routes (protocol kernel) come back with their address and are ignored."""

    def keys(routes: list[dict[str, Any]]) -> set[tuple[str, str, str, str]]:
        return {_route_key(r) for r in routes if r.get("protocol") != "kernel"}

    want, have = keys(wanted), keys(live)
    cmds = []
    for dst, gw, dev, _scope in sorted(have - want):
        cmds.append(f"ip route del {dst}" + (f" via {gw}" if gw else "") + f" dev {dev}")
    for dst, gw, dev, scope in sorted(want - have):
        cmd = f"ip route replace {dst}" + (f" via {gw}" if gw else "") + f" dev {dev}"
        if scope and scope != "global" and not gw:
            cmd += f" scope {scope}"
        cmds.append(cmd)
    return cmds


# ---- capabilities --------------------------------------------------------


class _Base:
    def __init__(self, transport: DockerExecTransport, port_map: PortMap) -> None:
        self.t = transport
        self.port_map = port_map

    def real(self, logical: str) -> str:
        return self.port_map[logical]

    def run(self, cmd: str, **kw: Any) -> CommandResult:
        return self.t.run(cmd, **kw)


class FrrOspf(_Base, Ospf):
    def neighbors(self) -> list[OspfNeighbor]:
        data = self.run("vtysh -c 'show ip ospf neighbor json'").check().json()
        return parse_frr_neighbors(data, self.port_map)

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
        """FRR config text. Backend-internal (snapshots); not part of the Ospf interface."""
        return self.run("vtysh -c 'show running-config'").check().stdout

    def interface_settings(self) -> dict[str, OspfInterface]:
        return parse_frr_interfaces(self.running_config(), self.port_map)

    def set_interface_area(self, port: str, area: int | None) -> None:
        ifname = self.port_map.real(port)
        if area is None:
            current = self.interface_settings().get(port)
            if current is None or current.area is None:
                return  # already out of OSPF
            change = f"no ip ospf area {current.area}"
        else:
            change = f"ip ospf area {area}"
        args = " ".join(f"-c {shlex.quote(c)}" for c in ("conf t", f"interface {ifname}", change))
        self.run(f"vtysh {args}").check()


class LinuxBridge(_Base, Bridge):
    def vlan_membership(self) -> dict[str, PortVlans]:
        return parse_bridge_vlans(self.run("bridge -j vlan show").check().json(), self.port_map)

    def set_access_vlan(self, port: str, vlan: int) -> None:
        self.set_port_vlans(port, PortVlans("access", (vlan,)))

    def set_port_vlans(self, port: str, wanted: PortVlans) -> None:
        """Replace the port's VLANs: access = one untagged PVID, trunk = tagged VLANs.
        Backend-internal (also used by snapshot restore); not part of the Bridge interface."""
        ifname = self.real(port)
        current = self.vlan_membership().get(port)
        for vid in current.vlans if current else ():
            self.run(f"bridge vlan del dev {ifname} vid {vid}").check()
        extra = " pvid untagged" if wanted.mode == "access" else ""
        for vid in wanted.vlans:
            self.run(f"bridge vlan add dev {ifname} vid {vid}{extra}").check()


class LinuxLinks(_Base, Links):
    def set_link(self, port: str, up: bool) -> None:
        self.run(f"ip link set {self.real(port)} {'up' if up else 'down'}").check()

    def link_state(self, port: str) -> LinkState:
        (info,) = self.run(f"ip -j link show {self.real(port)}").check().json()
        return _link_dict(port, info)

    def link_states(self, ports: list[str]) -> dict[str, LinkState]:
        """All requested ports from one `ip -j link show` (instead of one call per port).
        Ports missing from the container are left out."""
        by_name = {i.get("ifname"): i for i in self.run("ip -j link show").check().json() or []}
        return {p: _link_dict(p, by_name[self.real(p)]) for p in ports if self.real(p) in by_name}


def _link_dict(port: str, info: dict[str, Any]) -> LinkState:
    """`ip -j link` entry -> LinkState. Admin up = the UP flag. Operationally up =
    operstate UP, or UNKNOWN (veth and other drivers that do not report carrier)."""
    operstate = str(info.get("operstate", "?"))
    return LinkState(
        port=port,
        ifname=str(info["ifname"]),
        admin_up="UP" in info.get("flags", []),
        oper_up=operstate in ("UP", "UNKNOWN"),
        detail=f"operstate {operstate}",
    )


class LinuxShell(_Base, Shell):
    def __init__(self, transport: DockerExecTransport, port_map: PortMap, device: str) -> None:
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
        result = parse_ping(self.device, dst, res.stdout)
        # ping exits 0 (replies), 1 (no replies) or 2+ (error). No summary line, or
        # nothing sent, means it never probed: that must not read as "isolated".
        if res.rc >= 2 or result.sent == 0:
            detail = (res.stderr or res.stdout).strip().splitlines()
            reason = f"ping did not run (rc={res.rc}): {detail[-1] if detail else 'no output'}"
            return replace(result, sent=0, received=0, error=reason)
        return result

    def add_host_route(self, prefix: str, port: str = "p1") -> None:
        self.run(f"ip route replace {prefix} dev {self.real(port)} scope link").check()

    def del_host_route(self, prefix: str, port: str = "p1") -> None:
        self.run(f"ip route del {prefix} dev {self.real(port)}")

    def flush_neighbors(self, port: str = "p1") -> None:
        self.run(f"ip neigh flush dev {self.real(port)}")

    def neighbors(self) -> list[dict[str, str]]:
        return list(self.run("ip -j neigh show").check().json() or [])

    def route_table(self) -> list[dict[str, Any]]:
        """Backend-internal: the host's routes, for snapshots."""
        return list(self.run("ip -j route show").check().json() or [])

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
            f"tcpdump -nn -r {handle.remote_path} -c 200 2>/dev/null"
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


class LocalSnapshot(Snapshot):
    """A device's links, bridge VLANs, FRR config and host routes (ADR-0003).

    Built from the device's own adapters; a part is None when the role lacks it.
    Restore order: links, bridge, FRR, host routes (routes need their link up).
    Each part changes nothing when it already matches and verifies FRR afterwards.
    """

    def __init__(
        self,
        device: str,
        links: LinuxLinks,
        bridge: LinuxBridge | None = None,
        ospf: FrrOspf | None = None,
        host: LinuxShell | None = None,
    ) -> None:
        self.device = device
        self.links, self.bridge, self.ospf, self.host = links, bridge, ospf, host

    def take(self) -> dict[str, Any]:
        ports = list(self.links.port_map)
        state: dict[str, Any] = {
            "links": {
                p: "up" if s.admin_up else "down" for p, s in self.links.link_states(ports).items()
            }
        }
        if self.bridge is not None:
            # intended ports only: restore puts those back; an extra port is drift to report
            state["bridge"] = {
                p: [pv.mode, list(pv.vlans)]
                for p, pv in self.bridge.vlan_membership().items()
                if p in self.bridge.port_map
            }
        if self.ospf is not None:
            state["frr"] = self.ospf.running_config()
        if self.host is not None:
            state["host_routes"] = self.host.route_table()
        return state

    def restore_to(self, state: dict[str, Any]) -> None:
        self._restore_links(state["links"])
        if self.bridge is not None:
            self._restore_bridge(self.bridge, state["bridge"])
        if self.ospf is not None:
            self._restore_frr(self.ospf, state["frr"])
        if self.host is not None:
            for cmd in host_route_diff(state["host_routes"], self.host.route_table()):
                self.host.run(cmd).check()

    def _restore_links(self, wanted: dict[str, str]) -> None:
        live = self.links.link_states(list(wanted))
        for port, admin in wanted.items():
            if port not in live:
                raise TransportError(f"{self.device}: port {port} is gone")
            if live[port].admin_up != (admin == "up"):
                self.links.set_link(port, admin == "up")

    @staticmethod
    def _restore_bridge(bridge: LinuxBridge, wanted: dict[str, list[Any]]) -> None:
        live = bridge.vlan_membership()
        for port, (mode, vlans) in wanted.items():
            target = PortVlans(mode, tuple(vlans))
            if live.get(port) != target:
                bridge.set_port_vlans(port, target)

    def _restore_frr(self, ospf: FrrOspf, wanted: str) -> None:
        """Apply only the differing lines through vtysh, then re-read and verify.

        Does not depend on frr-reload.py (not usable in the FRR container image).
        """
        steps = frr_config_diff(wanted, ospf.running_config())
        if not steps:
            return
        for step in steps:
            args = " ".join(f"-c {shlex.quote(c)}" for c in ["conf t", *step])
            res = ospf.run(f"vtysh {args}")
            if not res.ok:
                raise TransportError(
                    f"{self.device}: restore step {step} failed: "
                    f"{(res.stdout + res.stderr).strip()[-300:]}"
                )
        remaining = frr_config_diff(wanted, ospf.running_config())
        if remaining:
            raise TransportError(f"{self.device}: config still differs after restore: {remaining}")


# ---- backend -------------------------------------------------------------


class LocalFactory(CapabilityFactory):
    """Adapters of the local backend: Linux links and shell, FRR, Linux bridge, all over
    `docker exec` into the node's container. Switches get a shell too (captures)."""

    def __init__(self, container: Callable[[str], str]) -> None:
        self.container = container
        self._transports: dict[str, DockerExecTransport] = {}
        self._links: dict[str, LinuxLinks] = {}
        self._ospf: dict[str, FrrOspf] = {}
        self._bridge: dict[str, LinuxBridge] = {}
        self._shell: dict[str, LinuxShell] = {}

    def _t(self, spec: DeviceSpec) -> DockerExecTransport:
        if spec.name not in self._transports:
            self._transports[spec.name] = DockerExecTransport(self.container(spec.name), spec.log)
        return self._transports[spec.name]

    def links(self, spec: DeviceSpec) -> Links:
        self._links[spec.name] = LinuxLinks(self._t(spec), spec.ports)
        return self._links[spec.name]

    def ospf(self, spec: DeviceSpec) -> Ospf:
        self._ospf[spec.name] = FrrOspf(self._t(spec), spec.ports)
        return self._ospf[spec.name]

    def bridge(self, spec: DeviceSpec) -> Bridge:
        self._bridge[spec.name] = LinuxBridge(self._t(spec), spec.ports)
        return self._bridge[spec.name]

    def shell(self, spec: DeviceSpec) -> Shell:
        self._shell[spec.name] = LinuxShell(self._t(spec), spec.ports, spec.name)
        return self._shell[spec.name]

    def snapshot(self, spec: DeviceSpec, built: dict[type, object]) -> Snapshot:
        n = spec.name
        return LocalSnapshot(
            n,
            self._links[n],
            self._bridge.get(n),
            self._ospf.get(n),
            self._shell.get(n) if spec.role == "host" else None,  # host routes: hosts only
        )


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

    def render(self) -> Path:
        return render(self.intent, self.settings["images"], self.build_dir, self.prefix)

    def deploy(self, fresh: bool = False) -> bool:
        self.render()
        if self.is_running() and not fresh:
            built_from = self.state.deployed_intent()
            missing = self.missing_ports()
            if built_from == self.intent.digest and not missing:
                return False
            if missing:
                print(f"lab is running but degraded (missing {', '.join(missing)}); redeploying")
            else:
                print(
                    f"lab was built from intent {built_from or 'unknown'}, "
                    f"intent is now {self.intent.digest}; redeploying"
                )
            self._devices = None
        subprocess.run(
            [*self.clab, "deploy", "-t", str(self.topology_file), "--reconfigure"], check=True
        )
        self.state.set_deployed_intent(self.intent.digest)
        return True

    def destroy(self) -> None:
        subprocess.run(
            [*self.clab, "destroy", "-t", str(self.topology_file), "--cleanup"], check=False
        )

    # devices
    def devices(self) -> dict[str, Device]:
        if self._devices is None:
            factory = LocalFactory(self.container)
            self._devices = assemble(self.intent, self.name, self.prefix, factory)
        return self._devices

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
