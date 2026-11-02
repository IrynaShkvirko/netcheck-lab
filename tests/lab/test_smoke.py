"""Smoke: the lab is up and every intended link is operational."""

import pytest

from netcheck.capabilities import Links
from netcheck.lab import Lab

pytestmark = pytest.mark.smoke


def test_every_intended_port_is_up(lab: Lab) -> None:
    down = []
    for dev in lab.devices.values():
        if not dev.has(Links):
            continue
        for port in dev.port_map:
            state, _ = lab.timing.wait_until(
                "interface_up",
                lambda d=dev, p=port: d.links.link_state(p),
                lambda s: s["admin"] == "up" and s["operstate"] in ("UP", "UNKNOWN"),
            )
            if state["admin"] != "up":
                down.append(f"{dev.name}:{port}")
    assert not down, f"ports not up: {down}"


def test_routers_run_ospf(lab: Lab) -> None:
    for r in lab.by_role("router"):
        assert "router ospf" in r.ospf.running_config(), f"{r.name} has no OSPF process"
