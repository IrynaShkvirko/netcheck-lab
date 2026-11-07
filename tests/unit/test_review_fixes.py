"""Regression tests for the code-review fixes. Each one failed before its fix."""

from __future__ import annotations

import pytest

from netcheck.assertions import assert_isolated
from netcheck.backends.local import LinuxShell
from netcheck.capabilities import PingResult, PortVlans, Route
from netcheck.convergence import measure_reconvergence
from netcheck.devicelog import DeviceLog
from netcheck.ports import PortMap
from netcheck.report import IntentViolation
from netcheck.transport.base import CommandResult, TransportError


class _StubTransport:
    def __init__(self, stdout: str, rc: int, stderr: str = "") -> None:
        self.log = DeviceLog("h1")
        self.result = CommandResult("ping", stdout, stderr, rc, target="clab-x-h1")

    def run(self, cmd: str, **_: object) -> CommandResult:
        return self.result


def _shell(stdout: str, rc: int, stderr: str = "") -> LinuxShell:
    return LinuxShell(_StubTransport(stdout, rc, stderr), {"p1": "eth1"}, "h1")  # type: ignore[arg-type]


# ---- #1: a ping that never ran must not look like "no replies" ----


def test_ping_that_did_not_run_raises() -> None:
    with pytest.raises(TransportError, match="did not run"):
        _shell("", 1, "Error: No such container: clab-x-h1").ping("10.0.20.13")


def test_ping_error_exit_code_raises() -> None:
    with pytest.raises(TransportError, match="did not run"):
        _shell("ping: sendmsg: Network is unreachable\n", 2).ping("10.0.20.13")


def test_ping_with_no_replies_is_a_result_not_an_error() -> None:
    r = _shell("20 packets transmitted, 0 received, 100% packet loss\n", 1).ping("10.0.20.13")
    assert (r.sent, r.received) == (20, 0)


def test_isolation_with_no_probes_sent_does_not_pass(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    monkeypatch.setattr(
        lab["h1"].shell, "ping", lambda *a, **k: PingResult("h1", "10.0.20.13", 0, 0)
    )
    with pytest.raises(IntentViolation, match="inconclusive"):
        assert_isolated(lab["h1"], lab["h3"])


# ---- #4: failover must start from the primary path ----


def test_failover_requires_primary_path_before_failure(fake) -> None:  # type: ignore[no-untyped-def]
    lab, parts = fake
    parts["ospf_r1"].rib = {
        "10.0.30.0/24": Route("10.0.30.0/24", "ospf", ("10.0.12.3",), True)  # already on backup
    }
    with pytest.raises(IntentViolation) as exc:
        measure_reconvergence(
            check="reconverge_local_link",
            src=lab["h1"],
            dst=lab["h4"],
            router=lab["r1"],
            prefix="10.0.30.0/24",
            expected_next_hop="10.0.12.3",
            before_next_hop="10.0.12.1",
            action=lambda: None,
        )
    assert exc.value.report.verdict.startswith("before the failure, r1 must route 10.0.30.0/24")


# ---- #2: a baseline is only taken from a freshly built lab ----


def test_reused_lab_keeps_existing_baseline(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    backend = lab.backend
    backend.state.set_baseline("baseline-original")
    backend.state.add_fault("wrong-vlan", "sw1:p3 moved")
    deployed, baseline = backend.up()
    assert (deployed, baseline) == (False, "baseline-original")  # the fault is not captured
    assert backend.state.active_faults()  # and it is still reported


def test_fresh_deploy_takes_new_baseline_and_clears_faults(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    backend = lab.backend
    backend.state.set_baseline("baseline-original")
    backend.state.add_fault("wrong-vlan", "sw1:p3 moved")
    deployed, baseline = backend.up(fresh=True)
    assert deployed and baseline.startswith("baseline-") and baseline != "baseline-original"
    assert backend.state.baseline() == baseline
    assert backend.state.active_faults() == []


def test_first_run_without_baseline_takes_one(fake) -> None:  # type: ignore[no-untyped-def]
    lab, _ = fake
    _, baseline = lab.backend.up()
    assert lab.backend.state.baseline() == baseline


def test_prune_snapshots_keeps_baseline(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import os

    from netcheck.labstate import LabState

    state = LabState(tmp_path)
    state.snapshots.mkdir(parents=True)
    for i in range(6):
        f = state.snapshots / f"s{i}.json"
        f.write_text("{}")
        os.utime(f, (i, i))  # s0 oldest
    state.set_baseline("s0")
    deleted = state.prune_snapshots(keep=2)
    assert sorted(p.stem for p in deleted) == ["s1", "s2", "s3"]
    assert sorted(p.stem for p in state.snapshots.glob("*.json")) == ["s0", "s4", "s5"]


# ---- #3, #11, #5: teardown failures are visible; stale artifacts go; no parallel ----

_CONFTEST = """
import sys
sys.path.insert(0, {repo!r})
from pathlib import Path
import pytest
from netcheck.intent import load_intent
from netcheck.lab import set_current
from tests.unit.fakes import build_fake_lab

@pytest.fixture(scope="session")
def lab(tmp_path_factory):
    art = Path("art").resolve()  # fixed per pytester dir, so a second run sees the first's files
    art.mkdir(exist_ok=True)
    the_lab, _ = build_fake_lab(load_intent({intent!r}), art)
    the_lab["r1"].snapshot.fail_with = {restore_error}
    token = set_current(the_lab)
    yield the_lab
    token.var.reset(token)
"""


def _plugin_run(pytester, restore_error: str, test_body: str, *args: str):  # type: ignore[no-untyped-def]
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    pytester.makeconftest(
        _CONFTEST.format(
            repo=str(repo), intent=str(repo / "intent/lab.yaml"), restore_error=restore_error
        )
    )
    pytester.makepyfile(test_body)
    return pytester.runpytest_inprocess("-p", "netcheck.pytest_plugin", *args)


def test_failed_cleanup_shows_as_error_not_pass(pytester) -> None:  # type: ignore[no-untyped-def]
    result = _plugin_run(
        pytester,
        "None",
        "def test_x(lab):\n    lab.cleanup.register('undo that breaks', lambda: 1 / 0)\n",
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        ["*ERROR at teardown of test_x*", "*cleanup failed after the test*", "*undo that breaks*"]
    )


def test_failed_restore_stops_the_session(pytester) -> None:  # type: ignore[no-untyped-def]
    result = _plugin_run(
        pytester,
        "RuntimeError('frr gone')",
        "def test_a(lab):\n    pass\n\ndef test_b(lab):\n    pass\n",
    )
    assert result.ret == 3
    result.stdout.fnmatch_lines(["*lab restore failed after*test_a*"])
    assert (
        "test_b" not in "".join(result.outlines).split("lab restore failed")[0].split("test_a")[-1]
    )


def test_stale_artifacts_are_removed(pytester) -> None:  # type: ignore[no-untyped-def]
    body = (
        "import os\n"
        "def test_x(lab):\n"
        "    stale = lab.test_artifacts / 'old-report.txt'\n"
        "    if os.environ.get('SECOND_RUN'):\n"
        "        assert not stale.exists(), 'artifact from the previous run is still there'\n"
        "    else:\n"
        "        lab.test_artifacts.mkdir(parents=True, exist_ok=True)\n"
        "        stale.write_text('from run 1')\n"
    )
    _plugin_run(pytester, "None", body).assert_outcomes(passed=1)
    import os

    os.environ["SECOND_RUN"] = "1"
    try:
        pytester.runpytest_inprocess("-p", "netcheck.pytest_plugin").assert_outcomes(passed=1)
    finally:
        del os.environ["SECOND_RUN"]


def test_parallel_workers_are_refused(pytester, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    result = _plugin_run(pytester, "None", "def test_x(lab):\n    pass\n")
    assert result.ret == 4
    result.stdout.fnmatch_lines(["*must run serially*"])


# ---- #6: an error while collecting evidence must not replace the verdict ----


def test_evidence_failure_keeps_the_verdict(fake, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions import assert_ospf_neighbor_full
    from netcheck.transport.base import TransportError as TE

    lab, parts = fake
    # r2 is unreachable: its neighbor table cannot be read for the report
    monkeypatch.setattr(parts["ospf_r2"], "neighbors", lambda: (_ for _ in ()).throw(TE("r2 gone")))
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_neighbor_full(lab["r1"], lab["r2"], port="p2")
    report = exc.value.report
    assert report.verdict.startswith("r1 must have a Full OSPF adjacency with r2")
    assert any("r2 gone" in ln for ln in report.evidence["evidence unavailable"])


# ---- #7, #14: OSPF settings come normalized from the backend; the fault uses them ----


def test_missing_ospf_fault_goes_through_the_capability(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.faults import missing_ospf_network

    lab, parts = fake
    msg = missing_ospf_network(lab.backend)
    assert msg.startswith("r2:p3 (eth3, 10.0.30.1/24) removed from OSPF area 0")
    assert parts["ospf_r2"].area_overrides == {"p3": None}


def test_assertions_do_not_parse_nos_text() -> None:
    """ADR-0002: no FRR syntax in the backend-neutral assertions."""
    from pathlib import Path

    import netcheck.assertions as pkg

    for f in Path(pkg.__file__).parent.glob("*.py"):
        text = f.read_text()
        for frr in ("running_config", "ip ospf area ", "vtysh"):
            assert frr not in text, f"{f.name} contains NOS-specific {frr!r}"


def test_same_area_accepts_both_frr_formats() -> None:
    from netcheck.assertions.ospf import same_area

    assert same_area("0", 0) and same_area("0.0.0.0", 0) and same_area("0.0.1.0", 256)
    assert not same_area(None, 0) and not same_area("1", 0)


# ---- #9: intent validation reports every problem, up front ----


def test_intent_validation_catches_bad_values(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from netcheck.intent import IntentError, load_intent

    (tmp_path / "bad.yaml").write_text(
        """
name: bad
ospf: {hello_interval: 4, dead_interval: 4}
nodes:
  r1: {role: router, router_id: 1.1.1.1}
  r2: {role: router, router_id: 1.1.1.1}
  h1: {role: host, ip: "10.0.10.11; rm -rf /", gateway: 10.0.99.1}
  h2: {role: host, ip: 10.0.10.12/24, gateway: 10.0.20.1}
  x1: {role: firewall}
vlans: {}
links:
  - {a: {node: h2, port: eth1}, b: {node: r1, port: p1, ip: 10.0.10.1/24}}
  - {a: {node: r1, port: p2, ip: 10.0.12.0/31}, b: {node: r2, port: p1, ip: 10.0.12.1/31},
     ospf_cost: 0}
expect:
  reachable: [[h2, h2]]
"""
    )
    with pytest.raises(IntentError) as exc:
        load_intent(tmp_path / "bad.yaml")
    msg = str(exc.value)
    for expected in (
        "hello_interval < dead_interval",
        "router_id 1.1.1.1 also used by r1",
        "host h1 ip: '10.0.10.11; rm -rf /' is not an address",
        "host h1: must have exactly one link, has 0",
        "gateway 10.0.20.1 is outside 10.0.10.0/24",
        "role 'firewall'",
        "port 'eth1' must look like p1",
        "ospf_cost must be a positive integer",
        "names the same host twice",
    ):
        assert expected in msg, expected


# ---- #10: one link read per device ----


def test_link_states_reads_all_ports_in_one_call() -> None:
    import json

    from netcheck.backends.local import LinuxLinks

    calls: list[str] = []

    class T:
        log = DeviceLog("r1")

        def run(self, cmd: str, **_: object) -> CommandResult:
            calls.append(cmd)
            data = [
                {"ifname": "eth1", "operstate": "UP", "flags": ["UP"]},
                {"ifname": "eth2", "operstate": "DOWN", "flags": []},
            ]
            return CommandResult(cmd, json.dumps(data), "", 0)

    links = LinuxLinks(T(), {"p1": "eth1", "p2": "eth2", "p3": "eth3"})  # type: ignore[arg-type]
    states = links.link_states(["p1", "p2", "p3"])
    assert calls == ["ip -j link show"]
    assert states["p1"]["admin"] == "up" and states["p2"]["admin"] == "down"
    assert "p3" not in states  # missing in the container: reported as missing, not crashing


# ---- low items ----


def test_bridge_port_without_vlans_is_none_not_trunk() -> None:
    from netcheck.backends.local import parse_bridge_vlans

    out = parse_bridge_vlans([{"ifname": "eth3", "vlans": []}], PortMap({"p3": "eth3"}))
    assert (out["p3"].mode, out["p3"].vlans) == ("none", ())


def test_device_log_is_bounded_and_marks_stay_valid() -> None:
    import time

    log = DeviceLog("r1", max_entries=3)
    for i in range(2):
        log.record("exec", f"cmd{i}", "ok", 0, time.time())
    mark = log.mark()
    for i in range(2, 6):
        log.record("exec", f"cmd{i}", "x" * 20000, 0, time.time())
    assert len(log.entries) == 3
    assert [e.request for e in log.since(mark)] == ["cmd3", "cmd4", "cmd5"]
    assert log.entries[-1].response.endswith("chars cut]")


def test_api_bodies_are_redacted_in_logs() -> None:
    from netcheck.transport.nvue_rest import redact

    body = {
        "system": {"hostname": "sw1", "api": {"password": "hunter2", "port": 8765}},
        "users": [{"name": "cumulus", "ssh-key": "AAAA"}],
    }
    out = redact(body)
    assert out["system"]["api"] == {"password": "***", "port": 8765}
    assert out["users"][0] == {"name": "cumulus", "ssh-key": "***"}
    assert out["system"]["hostname"] == "sw1"


# ---- readable lab tests: steps, and the real tests/lab files run on fakes ----

_LAB_CONFTEST = """
import sys
sys.path.insert(0, {repo!r})
from pathlib import Path
import pytest
from netcheck.intent import load_intent
from netcheck.lab import set_current
from netcheck.capabilities import PortVlans
from tests.unit.fakes import build_fake_lab, make_healthy

@pytest.fixture(scope="session")
def lab(tmp_path_factory):
    the_lab, parts = build_fake_lab(load_intent({intent!r}),
                                    Path(str(tmp_path_factory.mktemp("art"))))
    make_healthy(the_lab, parts)
    for port, vlan in {bad_ports!r}.items():  # injected wrong-VLAN faults
        parts["bridge"].ports[port] = PortVlans("access", (vlan,))
    token = set_current(the_lab)
    yield the_lab
    token.var.reset(token)
"""


def _run_lab_suite(  # type: ignore[no-untyped-def]
    pytester, wrong_vlan: bool = False, *args: str, bad_ports: dict[str, int] | None = None
):
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    lab_dir = repo / "tests" / "lab"
    if bad_ports is None:
        bad_ports = {"p3": 10} if wrong_vlan else {}
    conftest = (lab_dir / "conftest.py").read_text() + _LAB_CONFTEST.format(
        repo=str(repo), intent=str(repo / "intent/lab.yaml"), bad_ports=bad_ports
    )
    pytester.makeconftest(conftest)
    for f in lab_dir.glob("test_*.py"):
        pytester.makepyfile(**{f.stem: f.read_text()})
    return pytester.runpytest_inprocess(
        "-p", "netcheck.pytest_plugin", "--intent", str(repo / "intent/lab.yaml"), *args
    )


def test_lab_suite_passes_on_a_healthy_fake_lab(pytester) -> None:  # type: ignore[no-untyped-def]
    """The real tests/lab files, step by step, against a working (fake) lab."""
    result = _run_lab_suite(pytester)
    result.assert_outcomes(passed=16)


def test_failure_report_names_the_failed_step(pytester) -> None:  # type: ignore[no-untyped-def]
    result = _run_lab_suite(pytester, True, "-k", "isolated and h1-h3")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*INTENT VIOLATED: h1 (VLAN 10) and h3 (VLAN 20) must be isolated*",
            "*Steps:*",
            "*1. Look up where h1 and h3 are attached in the intent [ok*",
            "*2. Force h1 and h3 to talk directly over layer 2 [ok*",
            "*3. Start a packet capture on h3 [ok*",
            "*4. Send 5 pings from h1 to h3 [ok*",
            "*5. Check h3 never answered (5/5 replies) [FAILED*",
        ]
    )


def test_step_records_outcome_and_order(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.steps import step

    lab, _ = fake
    with step("first"):
        pass
    with pytest.raises(ZeroDivisionError), step("second"):
        1 / 0  # noqa: B018
    assert [(s.number, s.title, s.status) for s in lab.run.steps] == [
        (1, "first", "passed"),
        (2, "second", "failed"),
    ]


def test_check_next_hop_rejects_a_route_through_the_wrong_router(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions import check_next_hop

    lab, _ = fake
    route = Route("10.0.30.0/24", "ospf", ("10.0.99.9",), True)
    with pytest.raises(IntentViolation, match="r1 must reach 10.0.30.0/24 through r2"):
        check_next_hop(lab["r1"], route, "r2", {"10.0.12.1", "10.0.12.3"})


def test_interface_settings_mismatch_is_reported_per_port(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.assertions import assert_ospf_interface_matches_intent
    from tests.unit.fakes import make_healthy

    lab, parts = fake
    make_healthy(lab, parts)
    parts["ospf_r2"].config = parts["ospf_r2"].config.replace(
        "interface eth1\n ip address 10.0.12.1/31\n ip ospf area 0\n ip ospf dead-interval 4\n"
        " ip ospf hello-interval 1",
        "interface eth1\n ip address 10.0.12.1/31\n ip ospf area 0\n ip ospf dead-interval 4\n"
        " ip ospf hello-interval 5",
    )
    assert_ospf_interface_matches_intent(lab["r2"], "p2")  # backup link untouched
    with pytest.raises(IntentViolation) as exc:
        assert_ospf_interface_matches_intent(lab["r2"], "p1")
    (drift,) = exc.value.report.drift
    assert (drift.where, drift.expected, drift.actual) == (
        "r2:p1 (primary) hello interval",
        "1",
        "5",
    )


# ---- soft steps (continue_on_failure) -------------------------------------------------


def test_soft_step_records_the_failure_and_the_test_goes_on(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.steps import step

    lab, _ = fake
    reached = []
    for port in ("p1", "p2", "p3"):
        with step(f"check {port}", continue_on_failure=True):
            reached.append(port)
            assert port != "p2", f"{port} is wrong"
    assert reached == ["p1", "p2", "p3"]
    assert [(s.status, s.continued) for s in lab.run.steps] == [
        ("passed", False),
        ("failed", True),
        ("passed", False),
    ]
    assert lab.run.steps[1].line().startswith("2. check p2 [FAILED, continued, ")
    assert lab.run.steps[1].line().endswith(" - p2 is wrong")
    report = lab.run.soft_failure_report()
    assert report is not None
    assert (report.verdict, report.actual) == ("p2 is wrong", "1 of 3 failed (steps 2)")
    (evidence,) = report.evidence["step 2: check p2"]  # pytest's assert explanation is kept
    assert evidence.startswith("p2 is wrong")


def test_soft_step_still_stops_on_errors_that_are_not_check_failures(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.steps import step

    lab, _ = fake
    with pytest.raises(ConnectionError), step("read the switch", continue_on_failure=True):
        raise ConnectionError("switch unreachable")
    assert lab.run.steps[0].status == "failed" and not lab.run.steps[0].continued
    assert lab.run.soft_failure_report() is None


def test_no_soft_failures_means_no_report(fake) -> None:  # type: ignore[no-untyped-def]
    from netcheck.steps import step

    lab, _ = fake
    with step("fine", continue_on_failure=True):
        pass
    assert lab.run.soft_failure_report() is None


def test_vlan_test_reports_every_bad_port_in_one_run(pytester) -> None:  # type: ignore[no-untyped-def]
    """Two ports wrong: one failing test whose report lists both, and the good ports passed."""
    result = _run_lab_suite(
        pytester, False, "-k", "vlan_membership", bad_ports={"p3": 10, "p4": 10}
    )
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*INTENT VIOLATED: 2 checks failed: sw1:p3 must be access VLAN 20;"
            " sw1:p4 must be trunk VLAN 10,20*",
            "*Steps:*",
            "*3. Check sw1:p1 is access VLAN 10 [ok*",
            "*4. Check sw1:p2 is access VLAN 10 [ok*",
            "*5. Check sw1:p3 is access VLAN 20 [[]FAILED, continued, *] - actual: access VLAN 10*",
            "*6. Check sw1:p4 is trunk VLAN 10,20 [[]FAILED, continued*] - actual: access VLAN 10",
            "*7. Check sw1 has no bridge ports the intent does not list [ok*",
            "*Expected: all 5 checks pass*",
            "*Actual:   2 of 5 failed (steps 5, 6)*",
            "*Intent vs live:*",
            "*sw1:p3: expected 'access VLAN 20', actual 'access VLAN 10'*drift*",
            "*sw1:p4: expected 'trunk VLAN 10,20', actual 'access VLAN 10'*drift*",
        ]
    )


def test_vlan_test_reports_a_bridge_port_the_intent_does_not_list(pytester) -> None:  # type: ignore[no-untyped-def]
    """Someone plugs an unplanned port into sw1's VLAN 10: the intended ports still pass,
    the extra-port step fails and names it."""
    result = _run_lab_suite(pytester, False, "-k", "vlan_membership", bad_ports={"eth9": 10})
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*INTENT VIOLATED: sw1 must have no bridge ports outside the intent*",
            "*6. Check sw1:p4 is trunk VLAN 10,20 [ok*",
            "*7. Check sw1 has no bridge ports the intent does not list [[]FAILED, continued*"
            "] - actual: also eth9",
            "*Expected: only p1, p2, p3, p4*",
            "*sw1:eth9: expected 'absent', actual 'access VLAN 10'  (not in the intent)*drift*",
        ]
    )


def test_parser_keeps_unmapped_bridge_ports_under_their_real_name() -> None:
    from netcheck.backends.local import parse_bridge_vlans

    data = [
        {"ifname": "br0", "vlans": [{"vlan": 1, "flags": ["PVID", "Egress Untagged"]}]},
        {"ifname": "eth1", "vlans": [{"vlan": 10, "flags": ["PVID", "Egress Untagged"]}]},
        {"ifname": "eth9", "vlans": [{"vlan": 10, "flags": ["PVID", "Egress Untagged"]}]},
    ]
    out = parse_bridge_vlans(data, PortMap({"p1": "eth1"}))
    assert sorted(out) == ["eth9", "p1"]  # br0 is the bridge itself, not a port
    assert out["eth9"] == PortVlans("access", (10,))


def test_vlan_membership_reports_an_intended_port_missing_from_the_bridge() -> None:
    from netcheck.assertions import check_vlan_membership

    expected = {"p1": {"mode": "access", "vlans": [10]}, "p2": {"mode": "access", "vlans": [10]}}
    with pytest.raises(IntentViolation) as exc:
        check_vlan_membership("sw1", expected, {"p1": PortVlans("access", (10,))})
    (drift,) = exc.value.report.drift
    assert (drift.where, drift.actual, drift.note) == ("sw1:p2", "absent", "not on the bridge")
