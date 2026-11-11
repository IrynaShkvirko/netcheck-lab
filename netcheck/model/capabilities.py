"""Capability interfaces and normalized state types (ADR-0002).

Every backend returns the same shapes with *logical* port names, so assertions
compare live state to the intent without knowing the NOS.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

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
class OspfInterface:
    """OSPF settings of one interface, normalized across NOSes (ADR-0002).

    `name` is logical: a port ("p2"), or a VLAN sub-interface ("p1.10").
    None means "not configured" (the NOS default applies).
    """

    name: str
    addresses: tuple[str, ...] = ()
    area: str | None = None
    hello_interval: int | None = None
    dead_interval: int | None = None
    passive: bool = False
    cost: int | None = None
    network: str | None = None


@dataclass(frozen=True)
class PortVlans:
    mode: str  # access | trunk | none (in the bridge but carrying no VLAN)
    vlans: tuple[int, ...]


@dataclass(frozen=True)
class LinkState:
    """One port's link, normalized: each backend decides what "up" means for its NOS."""

    port: str  # logical
    ifname: str  # real interface name
    admin_up: bool
    oper_up: bool
    detail: str = ""  # the backend's own words, for evidence ("admin up, oper DOWN")

    def line(self) -> str:
        admin = "up" if self.admin_up else "down"
        oper = "up" if self.oper_up else "down"
        text = f"{self.port} ({self.ifname}) admin {admin}, oper {oper}"
        return f"{text} [{self.detail}]" if self.detail else text


@dataclass(frozen=True)
class PingResult:
    """The outcome of a ping. `error` says why no probe left the host: a ping that did
    not run is a result, never "no replies" (it must not read as isolated)."""

    src: str
    dst: str
    sent: int
    received: int
    raw: str = ""
    error: str = ""
    elapsed_s: float | None = None  # from first to last probe, when ping reports it

    @property
    def interval_s(self) -> float | None:
        """The interval the probes were really sent at (elapsed / gaps), or None. On a
        busy machine it is longer than asked for, and each lost probe stands for more time."""
        if self.elapsed_s is None or self.sent < 2:
            return None
        return self.elapsed_s / (self.sent - 1)

    @property
    def ran(self) -> bool:
        """At least one probe was sent."""
        return self.sent > 0 and not self.error

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
    def interface_settings(self) -> dict[str, OspfInterface]:
        """OSPF settings per logical interface. Assertions compare these with the
        intent; they never parse NOS config text themselves."""

    @abstractmethod
    def set_interface_area(self, port: str, area: int | None) -> None:
        """Put a logical interface in an OSPF area, or take it out (area=None)."""


class Bridge(ABC):
    @abstractmethod
    def vlan_membership(self) -> dict[str, PortVlans]: ...

    @abstractmethod
    def set_access_vlan(self, port: str, vlan: int) -> None: ...


class Links(ABC):
    @abstractmethod
    def set_link(self, port: str, up: bool) -> None: ...

    @abstractmethod
    def link_state(self, port: str) -> LinkState: ...

    def link_states(self, ports: list[str]) -> dict[str, LinkState]:
        """State of several ports. Backends override this to read them in one call;
        a port that does not exist is left out of the result."""
        return {p: self.link_state(p) for p in ports}


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
    ) -> PingResult:
        """Never raises because the ping could not run: returns a result with sent=0 and
        `error` set instead (see PingResult.ran)."""

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
    def stop_capture(self, handle: CaptureHandle, dest_dir: str) -> CaptureHandle:
        """Stop the capture, read its summary, copy the pcap to `dest_dir`. Calling it
        again on a stopped handle returns the handle unchanged (the cleanup registry
        stops every capture once more as a safety net)."""

    @abstractmethod
    def start_stream(self, dst: str, interval: float, deadline: float) -> str: ...

    @abstractmethod
    def stop_stream(self, token: str) -> PingResult: ...


class Snapshot(ABC):
    """A device's restorable state: configuration and link state (ADR-0003).

    The state is a JSON-serializable value that only the adapter which took it
    understands; the backend stores it without looking inside.
    """

    @abstractmethod
    def take(self) -> Any: ...

    @abstractmethod
    def restore_to(self, state: Any) -> None:
        """Bring the device back to `state`, verify it, raise if it still differs."""
