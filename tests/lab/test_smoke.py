"""Smoke: the lab is up and every intended link is operational."""

import pytest

from netcheck.assertions.ospf import same_area
from netcheck.model.capabilities import Links
from netcheck.run.lab import Lab
from netcheck.run.steps import step
from netcheck.run.timing import WaitTimeout

pytestmark = pytest.mark.smoke


def test_every_intended_port_is_up(lab: Lab) -> None:
    """All intended ports are admin-up and operationally up."""
    with step("List every port the intent connects, per device"):
        devices = [dev for dev in lab.devices.values() if dev.has(Links)]
        total = sum(len(dev.port_map) for dev in devices)

    def down_ports() -> list[str]:
        down = []
        for dev in devices:
            states = dev.links.link_states(list(dev.port_map))  # one read per device
            for port in dev.port_map:
                s = states.get(port)
                if s is None:
                    down.append(f"{dev.name}:{port} (missing)")
                elif not (s.admin_up and s.oper_up):
                    down.append(f"{dev.name}:{s.line()}")
        return down

    with step(f"Wait until all {total} ports are admin up and operationally up"):
        try:
            lab.timing.wait_until("interface_up", down_ports, lambda d: not d)
        except WaitTimeout as exc:
            still_down = exc.last if isinstance(exc.last, list) else []
            raise AssertionError(exc.describe(f"ports not up: {', '.join(still_down)}")) from exc


def test_routers_run_ospf(lab: Lab) -> None:
    """Every router has at least one interface in the intended OSPF area.

    One soft step per router, so a run reports every router without OSPF, not just the first.
    """
    area = lab.intent.ospf.area
    for router in lab.by_role("router"):
        with step(
            f"Read {router.name}'s OSPF interfaces and look for area {area}",
            continue_on_failure=True,
        ):
            interfaces = router.ospf.interface_settings()
            in_area = [name for name, s in interfaces.items() if same_area(s.area, area)]
            assert in_area, f"{router.name} has no interface in OSPF area {area}"
