"""Device = name + role + capabilities (composition, ADR-0002)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from netcheck.devices.devicelog import DeviceLog
from netcheck.model.capabilities import Bridge, Links, Ospf, Shell, Snapshot
from netcheck.model.ports import PortMap


class CapabilityMissing(LookupError):
    """The backend does not provide this capability for this device.

    The pytest layer turns it into a skip with the backend name in the reason.
    """


@dataclass
class Device:
    name: str
    role: str
    backend: str
    log: DeviceLog
    capabilities: dict[type, object] = field(default_factory=dict)
    port_map: PortMap = field(default_factory=PortMap)  # logical -> backend name
    version: str = "unknown"

    def cap(self, kind: type) -> object:
        """Look up a capability by its interface class (abstract classes allowed)."""
        impl = self.capabilities.get(kind)
        if impl is None:
            raise CapabilityMissing(
                f"{self.name} on backend '{self.backend}' has no {kind.__name__} capability"
            )
        return impl

    def has(self, kind: type) -> bool:
        return kind in self.capabilities

    # convenience accessors used by tests and assertions
    @property
    def ospf(self) -> Ospf:
        return cast(Ospf, self.cap(Ospf))

    @property
    def bridge(self) -> Bridge:
        return cast(Bridge, self.cap(Bridge))

    @property
    def links(self) -> Links:
        return cast(Links, self.cap(Links))

    @property
    def shell(self) -> Shell:
        return cast(Shell, self.cap(Shell))

    @property
    def snapshot(self) -> Snapshot:
        return cast(Snapshot, self.cap(Snapshot))

    def __repr__(self) -> str:
        return f"Device({self.name}, {self.role}, {self.backend})"
