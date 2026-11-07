"""Building every device of the lab from the intent (ADR-0001, ADR-0002).

One table says which capabilities a role may have; a backend's CapabilityFactory says
which of those it can provide, one method per capability. Every intent node becomes a
Device, also one the backend gives nothing: tests that need a missing capability are
then skipped (CapabilityMissing) instead of failing on an unknown device.
"""

from __future__ import annotations

from dataclasses import dataclass

from netcheck.capabilities import Bridge, Links, Ospf, Shell, Snapshot
from netcheck.device import Device
from netcheck.devicelog import DeviceLog
from netcheck.intent import Intent
from netcheck.ports import PortMap

# Capabilities each role may have; Snapshot comes last because it is built from the others.
ROLE_CAPABILITIES: dict[str, tuple[type, ...]] = {
    "router": (Links, Ospf, Snapshot),
    "switch": (Links, Bridge, Shell, Snapshot),
    "host": (Links, Shell, Snapshot),
}


@dataclass(frozen=True)
class DeviceSpec:
    """What a factory gets to build one device's adapters."""

    name: str
    role: str
    ports: PortMap
    log: DeviceLog


class CapabilityFactory:
    """A backend's adapters. Each method returns None when the backend cannot provide
    that capability for the device; a backend overrides only what it supports."""

    def links(self, spec: DeviceSpec) -> Links | None:
        return None

    def ospf(self, spec: DeviceSpec) -> Ospf | None:
        return None

    def bridge(self, spec: DeviceSpec) -> Bridge | None:
        return None

    def shell(self, spec: DeviceSpec) -> Shell | None:
        return None

    def snapshot(self, spec: DeviceSpec, built: dict[type, object]) -> Snapshot | None:
        """Called last, with the device's other adapters (by capability class)."""
        return None


def assemble(
    intent: Intent, backend: str, prefix: str, factory: CapabilityFactory
) -> dict[str, Device]:
    """A Device for every intent node, with the capabilities its role allows and the
    backend provides. Ports are named with the backend's prefix (eth, swp)."""
    devices = {}
    for name, node in intent.nodes.items():
        spec = DeviceSpec(name, node.role, PortMap.for_node(intent, name, prefix), DeviceLog(name))
        caps: dict[type, object] = {}
        for kind in ROLE_CAPABILITIES[node.role]:
            adapter: object | None
            if kind is Links:
                adapter = factory.links(spec)
            elif kind is Ospf:
                adapter = factory.ospf(spec)
            elif kind is Bridge:
                adapter = factory.bridge(spec)
            elif kind is Shell:
                adapter = factory.shell(spec)
            else:
                adapter = factory.snapshot(spec, dict(caps))
            if adapter is not None:
                caps[kind] = adapter
        devices[name] = Device(name, node.role, backend, spec.log, caps, spec.ports)
    return devices
