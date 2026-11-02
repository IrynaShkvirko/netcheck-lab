"""Capability interfaces and normalized state types (ADR-0002).

Every backend returns the same shapes with *logical* port names, so assertions
compare live state to the intent without knowing the NOS.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# ---- normalized state --------------------------------------------------


@dataclass(frozen=True)
class OspfNeighbor:
    router_id: str
    state: str  # Full, 2-Way, Init, ...
    port: str  # logical port


@dataclass(frozen=True)
class Route:
    prefix: str
    protocol: str
    next_hops: tuple[str, ...]
    installed: bool


@dataclass(frozen=True)
class PortVlans:
    mode: str  # access | trunk
    vlans: tuple[int, ...]


@dataclass(frozen=True)
class PingResult:
    src: str
    dst: str
    sent: int
    received: int
    raw: str = ""

    @property
    def loss(self) -> int:
        return self.sent - self.received


@dataclass
class CaptureHandle:
    device: str
    port: str
    remote_path: str
    pid: str
    local_path: str | None = None
    summary: list[str] = field(default_factory=list)
    stopped: bool = False


# ---- capability interfaces ----------------------------------------------


class Ospf(ABC):
    @abstractmethod
    def neighbors(self) -> list[OspfNeighbor]: ...

    @abstractmethod
    def routes(self) -> dict[str, Route]: ...

    @abstractmethod
    def lsdb_prefixes(self) -> set[str]: ...

    @abstractmethod
    def running_config(self) -> str: ...


class Bridge(ABC):
    @abstractmethod
    def vlan_membership(self) -> dict[str, PortVlans]: ...

    @abstractmethod
    def set_access_vlan(self, port: str, vlan: int) -> None: ...


class Links(ABC):
    @abstractmethod
    def set_link(self, port: str, up: bool) -> None: ...

    @abstractmethod
    def link_state(self, port: str) -> dict[str, str]: ...


class Shell(ABC):
    """Diagnostics from a host or device shell: ping, routes, captures."""

    @abstractmethod
    def ping(
        self,
        dst: str,
        *,
        count: int = 3,
        interval: float = 0.2,
        deadline: float = 4.0,
        src_port: str = "p1",
    ) -> PingResult: ...

    @abstractmethod
    def add_host_route(self, prefix: str, port: str = "p1") -> None: ...

    @abstractmethod
    def del_host_route(self, prefix: str, port: str = "p1") -> None: ...

    @abstractmethod
    def flush_neighbors(self, port: str = "p1") -> None: ...

    @abstractmethod
    def neighbors(self) -> list[dict[str, str]]: ...

    @abstractmethod
    def start_capture(self, port: str, bpf: str = "") -> CaptureHandle: ...

    @abstractmethod
    def stop_capture(self, handle: CaptureHandle, dest_dir: str) -> CaptureHandle: ...

    @abstractmethod
    def start_stream(self, dst: str, interval: float, deadline: float) -> str: ...

    @abstractmethod
    def stop_stream(self, token: str) -> PingResult: ...
