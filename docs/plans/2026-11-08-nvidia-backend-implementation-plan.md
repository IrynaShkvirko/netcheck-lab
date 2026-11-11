# Implementation Plan: NVIDIA backend (Cumulus Linux on DSX Air)

**Requirements:** `docs/requirements/nvidia-backend-requirements.md` (REQ-x.y below refer to it)
**Date:** 2026-11-08
**Branch per phase:** `nvidia-backend/<phase>-<short-name>`, one PR per phase (two for phase 1 if
the tracks finish apart).

## Goal

Make `--backend nvidia` deploy the intent as a DSX Air simulation (Cumulus switches and routers,
Ubuntu hosts) and run the unchanged lab suite on it, with snapshots, faults and timing as on the
local backend.

## What exists, what is missing

| Part | State |
| --- | --- |
| NVUE parsers (neighbors, routes before 5.11, OSPF interfaces, bridge, links) | done, unit-tested on doc-based samples |
| NVUE revision flow (`apply_changes`) | done, unit-tested |
| `NvueOspf`, `NvueBridge`, `NvueLinks`, `NvueFactory` | done (no `lsdb_prefixes`, no hosts, no snapshot) |
| `NvueRestTransport` | done (basic auth, TLS options, redaction) |
| `NvidiaAirBackend` | stub: `render`, `deploy`, `destroy`, `snapshot`, `restore` raise |
| Air API client, topology rendering, SSH access, NVUE snapshot, host adapters | missing |

## Architecture

```
NvidiaAirBackend (Backend)                      .netcheck/nvidia/simulation.json
  render()  -> air_topology.render_air()           {id, ssh_fqdn, ssh_port, intent, created}
  deploy()  -> AirApi: import, expire, start, wait ACTIVE
            -> OobAccess: wait for SSH, push NVUE config (merge), configure hosts
  destroy() -> AirApi.delete, forget record, close tunnels
  devices() -> assemble(intent, "nvidia", prefix_for_role, NvidiaFactory)
                 router/switch: NvueOspf | NvueBridge | NvueLinks | NvueSnapshot
                                over NvueRestTransport -> 127.0.0.1:<tunnel port>
                 switch shell, host links/shell/snapshot: LinuxShell | LinuxLinks |
                                LocalSnapshot over SshTransport
OobAccess (devices/transport/ssh.py)
  laptop --ssh--> <worker_fqdn>:<worker_port> (Air SSH service) = oob-mgmt-server
         --ssh -J--> cumulus@r1 / ubuntu@h1     (shell commands, scp)
         --ssh -J -L 127.0.0.1:N:127.0.0.1:8765--> cumulus@sw1   (NVUE tunnel)
```

Design choices this plan commits to (each recorded in ADR-0007, task 4.4):

- **One way in.** Everything goes through the Air SSH service on the oob-mgmt-server (REQ-3.1).
  NVUE stays on the switch's localhost; a per-device SSH tunnel reaches it (REQ-3.2). No NVUE
  listening-address change is needed.
- **Reuse the Linux adapters.** Ubuntu hosts and the Cumulus switch shell are Linux:
  `LinuxShell`, `LinuxLinks` and `LocalSnapshot` from `netcheck/backends/local.py` run unchanged
  over an `SshTransport` (it implements `CommandTransport`). Only the transport is new.
- **Thin Air client, no SDK.** `AirApi` speaks the v3 REST API through an `ApiTransport` with
  Bearer auth, so tests use a fake transport, as the NVUE adapters already do (ADR-0002 seam).
  The `air_sdk` package is not added.
- **NVUE snapshot = full applied config.** Take: `GET /nvue_v1/?rev=applied`. Restore: one
  revision that replaces the root with the stored config, then re-read and compare (REQ-5.2).
  The first configuration push merges (PATCH, no root DELETE) so the management interface the
  image ships with survives; the baseline is taken after it and so contains everything.

### Files to create

- `netcheck/devices/transport/rest.py`: `RestTransport` (shared HTTP + logging + redaction), auth by basic or Bearer
- `netcheck/backends/air_api.py`: `AirApi`, `AirError`, simulation states
- `netcheck/backends/air_topology.py`: intent -> Air topology JSON and per-device NVUE config (pure)
- `netcheck/devices/transport/ssh.py`: `SshEndpoint`, `SshTransport`, `SshTunnel`, `OobAccess`
- `tests/unit/fakes_air.py`: `FakeAirTransport`
- `tests/unit/test_air_api.py`, `test_air_topology.py`, `test_ssh_transport.py`,
  `test_nvue_snapshot.py`, `test_nvidia_backend.py`
- `.github/workflows/nvidia.yml`
- `docs/decisions/0007-nvidia-access-and-snapshots.md`

### Files to modify

- `netcheck/devices/transport/nvue_rest.py`: becomes a thin `RestTransport` with basic auth
- `netcheck/devices/assembly.py`: port prefix per role
- `netcheck/backends/nvidia_air.py`: `lsdb_prefixes`, 5.11+ routes if needed, `NvueSnapshot`,
  `NvidiaFactory` (hosts, switch shell), `NvidiaAirBackend` lifecycle
- `backends.yaml`, `timing.yaml`, `Makefile` (only if a target needs it), `README.md`,
  `GLOSSARY.md`, ADR-0001/0003/0004, `docs/decisions/README.md`
- `tests/unit/fixtures/nvue/**` (captured samples), `tests/unit/fakes_nvue.py`

---

## Phase 0: spikes on a real simulation (human with an Air account, ~half a day)

Phase 0 needs an NGC org with Air access and `NVIDIA_AIR_TOKEN`. It produces facts, not code.
Write each answer into `docs/research/2026-11-nvidia-air-spikes.md` (one section per spike,
with the request and the response trimmed and redacted). Phase 1 tracks A–E can start before
phase 0 ends; every place that depends on a spike says **[S#]**, and is a constant or one
function so the answer is a one-line change.

### S1: Topology import and lifecycle calls

1. Write a two-node JSON topology by hand (one Cumulus, one Ubuntu, one link, OOB on).
2. `POST /api/v3/simulations/parse/` with it: record the accepted link and service shapes.
3. `POST /api/v3/simulations/import/`, then the start call
   (`.../simulations/{id}/start/`, method as documented), poll `GET .../simulations/{id}/` and
   record every state seen until `ACTIVE`, with timestamps.
4. Record the exact image names for Cumulus (the release to pin) and Ubuntu.
5. `PATCH .../simulations/{id}/` with `expires_at`: record the accepted field and format.
6. `DELETE .../simulations/{id}/`, then `GET` it: record the status code (404 expected).

**Done when:** the spike file has the request body and response for each call, the list of
states, the boot duration, and the two image names.

### S2: SSH service on the oob-mgmt-server

1. Create the SSH service (in the import `services` list, or
   `POST /api/v3/simulations/nodes/interfaces/services/` on the oob-mgmt-server's outbound
   interface); record `worker_fqdn` and `worker_port` and when they become non-null.
2. With your public key in the Air profile: `ssh -p <port> ubuntu@<fqdn>` works.
3. From there: does `ssh cumulus@<switch>` work by hostname without a password? Does
   `ssh ubuntu@<host>`? Record what the images need for key login (a `file` or `shell`
   instruction at import that writes `authorized_keys`).
4. `ssh -J ubuntu@<fqdn>:<port> cumulus@<switch> -L 127.0.0.1:18765:127.0.0.1:8765 -N`, then
   `curl -k -u cumulus:<pw> https://127.0.0.1:18765/nvue_v1/system` answers.
5. On the Ubuntu host: `sudo -n true` works for `ubuntu`? `tcpdump` and `ping` are installed?
   Same for `cumulus` on the switch.

**Done when:** a single command from the laptop runs `ip -j link` on a host and on a switch
through the jump, and the NVUE curl through the tunnel returns JSON.

### S3: NVUE responses and the replace revision

1. On a configured switch and router, run `record_samples(transport, dest)` (existing function)
   and save the output under the spike folder.
2. Record the RIB shape on the pinned release (pre-5.11 tree or the newer one) and the LSDB path.
3. Take `GET /nvue_v1/?rev=applied`. Create a revision with `DELETE /nvue_v1/?rev=<r>` then
   `PATCH /nvue_v1/?rev=<r>` with that same body, apply it: does the switch keep its management
   access and end with an identical applied config? Record any key that differs between two
   `rev=applied` reads with no change in between (volatile keys must be ignored when comparing).
4. Record how NVUE writes a sub-interface (`swp1.10`) and an access port, from the applied config.

**Done when:** the replace revision is confirmed safe (or its failure mode is recorded), the
volatile keys are listed, and real samples exist for every `PATHS` entry.

---

## Phase 1: offline building blocks (five tracks in parallel, no Air needed)

Every task ends with `make check` green (ruff, mypy, unit tests). Tracks touch disjoint files;
the only shared file is `pyproject.toml` (no change expected).

### Track A: REST transport and Air API client

#### Task A1: extract `RestTransport`

**Files:** `netcheck/devices/transport/rest.py` (new), `netcheck/devices/transport/nvue_rest.py`

1. Move `SECRET_WORDS`, `redact` and the body of `NvueRestTransport.request` into
   `RestTransport(base_url, log, *, auth=None, headers=None, verify_tls=True, timeout=15.0)`.
   `kind="api"` log entries stay identical (request `"{method} {path}"`, redacted payload, status).
2. Add `"authorization"` to `SECRET_WORDS`; `redact` must also redact the `Authorization` header
   if headers are ever logged (they are not logged today: keep it that way).
3. `NvueRestTransport(host, user, password, log, port=8765, verify_tls=True, timeout=15.0)` keeps
   its signature and builds `RestTransport(f"https://{host}:{port}/nvue_v1", log,
   auth=(user, password), ...)`. Re-export `redact` from `nvue_rest` (tests import it there).

**Verification:**
- [ ] `uv run pytest tests/unit -q` passes unchanged
- [ ] `grep -n "requests" netcheck/devices/transport/nvue_rest.py` finds nothing (only `rest.py` imports it)

#### Task A2: `AirApi` with a fake transport

**Files:** `netcheck/backends/air_api.py` (new), `tests/unit/fakes_air.py` (new),
`tests/unit/test_air_api.py` (new)

1. In `air_api.py`:
   ```python
   AIR_URL = "https://api.dsx-air.nvidia.com/api/v3"
   ACTIVE, INACTIVE, INVALID = "ACTIVE", "INACTIVE", "INVALID"

   class AirError(RuntimeError):
       """The Air API refused a call or a simulation ended in a failed state."""

   def air_transport(token: str, log: DeviceLog) -> RestTransport:
       return RestTransport(AIR_URL, log, headers={"Authorization": f"Bearer {token}"}, timeout=30)

   class AirApi:
       def __init__(self, t: ApiTransport, *, sleep=time.sleep, clock=time.monotonic): ...
       def import_simulation(self, name: str, content: dict) -> str        # POST simulations/import/
       def simulation(self, sim_id: str) -> dict | None                   # GET; 404 -> None
       def set_expiry(self, sim_id: str, expires_at: datetime) -> None    # PATCH [S1]
       def start(self, sim_id: str) -> None                               # [S1] method/path
       def wait_for_state(self, sim_id, want, *, timeout, poll, error_states=(INVALID,)) -> dict
       def ssh_service(self, sim_id: str) -> tuple[str, int] | None       # (fqdn, port) [S2]
       def delete(self, sim_id: str) -> None                              # DELETE; 404 is fine
   ```
   Every non-2xx answer raises `AirError(f"{method} {path}: HTTP {status} {detail}")` using the
   API's `detail` field; `wait_for_state` raises `AirError` naming the simulation ID, the last
   state and the elapsed time on timeout or an error state (REQ-2.6).
2. `FakeAirTransport` in `fakes_air.py`: keeps simulations in a dict; `import` returns
   `{"id": "sim-1", "state": "IMPORTING"}`; each `GET` advances a scripted state list
   (`states=["IMPORTING", "INACTIVE"]`, then after `start` `["BOOTING", "ACTIVE"]`); `DELETE`
   removes; `status_for[(method, path)]` forces an HTTP status (same idea as `FakeNvueTransport`).
3. Tests (one behaviour each): import returns the ID and sends `format: JSON` and the name;
   `simulation` of an unknown ID is None; `wait_for_state` reaches ACTIVE through the scripted
   states with an injected clock and sleep; `INVALID` raises with the ID in the message; timeout
   raises with the last state; a 403 raises `AirError` carrying `detail`; delete of a missing
   simulation passes.

**Verification:**
- [ ] `uv run pytest tests/unit/test_air_api.py -q` passes, no network access (no real transport built)
- [ ] the token never appears in `FakeAirTransport.calls` (the fake sees bodies, not headers)

### Track B: topology and NVUE configuration from the intent

#### Task B1: port prefix per role in the assembly

**File:** `netcheck/devices/assembly.py`, `tests/unit/test_assembly.py`

1. `assemble(intent, backend, prefix, factory)` accepts `prefix: str | Mapping[str, str]`; a
   mapping is looked up by role (`{"router": "swp", "switch": "swp", "host": "eth"}`). A `str`
   keeps today's behaviour for every caller.
2. Test: with the mapping, `devices["h1"].port_map["p1"] == "eth1"` and
   `devices["r1"].port_map["p1"] == "swp1"`.

**Verification:**
- [ ] existing assembly tests pass unchanged; the new test passes

#### Task B2: per-device NVUE configuration

**Files:** `netcheck/backends/air_topology.py` (new), `tests/unit/test_air_topology.py` (new)

1. `nvue_router_config(intent, router, prefix="swp") -> dict`: from
   `Expected(intent).ospf_interfaces(router)` write, per interface, the keys
   `parse_nvue_ospf_interfaces` reads: `ip.address.<cidr>: {}`, `router.ospf.area` (string),
   `router.ospf.timers.hello-interval|dead-interval`, `router.ospf.network-type`,
   `router.ospf.passive: "on"` for passive, `router.ospf.cost` when set. A VLAN sub-interface
   (`p1.10`) becomes `swp1.10` with `type: sub`, `base-interface: swp1`, `vlan: 10` **[S3]**.
   Add `router.ospf.enable: "on"` and `router-id` under `vrf.default.router.ospf` and
   `system.hostname`.
2. `nvue_switch_config(intent, switch, prefix="swp") -> dict`: `bridge.domain.br_default.vlan`
   with every VLAN of the intent; per access port `bridge.domain.br_default.access: <vid>`;
   per trunk port `bridge.domain.br_default.vlan: {"10": {}, "20": {}}`; every intended port
   `link.state: {"up": {}}`.
3. Round-trip tests (REQ-1.6), the cross-backend contract:
   ```python
   def test_router_config_round_trips_through_the_nvue_parser(intent):
       for r in ("r1", "r2"):
           cfg = nvue_router_config(intent, r)
           ports = PortMap.for_node(intent, r, "swp")
           assert parse_nvue_ospf_interfaces(cfg["interface"], ports) == Expected(
               intent
           ).ospf_interfaces(r)
   ```
   and the same for the switch with `parse_nvue_bridge(cfg["interface"], cfg["bridge"]["domain"]["br_default"], ports)`
   against `Expected(intent).vlan_membership("sw1")`.

**Verification:**
- [ ] both round-trip tests pass; changing an OSPF timer in a copy of the intent changes the output

#### Task B3: Air topology JSON

**Files:** `netcheck/backends/air_topology.py`, `tests/unit/test_air_topology.py`

1. `air_topology(intent, settings) -> dict` returns the import body content **[S1]**:
   `nodes` (name -> `{"os": settings["images"][role], ...}`, CPU/memory from
   `settings.get("resources", {}).get(role)`), `links` (each intent link as two
   `{node, interface}` ends with real names from `PortMap`), `oob` enabled, `services` with one
   SSH service on the oob-mgmt-server (port 22) **[S2]**, and `instructions` that install the
   public key (`settings["ssh_public_key"]` file contents) for `cumulus` and `ubuntu` **[S2]**.
2. The NVUE password is never written into the topology: the instruction sets it from a
   placeholder that `deploy` fills right before the call, and the Air log redacts it
   (`password` is in `SECRET_WORDS`; name the field accordingly).
3. `render_air(intent, settings, out_dir) -> Path` writes `air-topology.json` (with the
   placeholder, never the password), `nvue/<device>.json` per router and switch, and
   `expected_state.json`; returns the topology path (REQ-1.5).
4. Tests: every intent node and link is present with `swp`/`eth` names; the SSH service is
   there; rendering twice gives identical files; no file contains the value of `NVUE_PASSWORD`
   set in the test environment.

**Verification:**
- [ ] `uv run python -m netcheck render --backend nvidia` writes `build/campus-mini/nvidia/` (after
      task D3 wires `render`)
- [ ] the generated JSON is accepted by `POST simulations/parse/` **[S1]** (manual, phase 2)

### Track C: SSH access

#### Task C1: `SshTransport`

**Files:** `netcheck/devices/transport/ssh.py` (new), `tests/unit/test_ssh_transport.py` (new)

1. ```python
   @dataclass(frozen=True)
   class SshEndpoint:
       jump_host: str          # Air worker FQDN
       jump_port: int
       jump_user: str          # ubuntu (oob-mgmt-server)
       known_hosts: Path       # .netcheck/nvidia/known_hosts (one file per simulation)
       control_dir: Path       # .netcheck/nvidia/ssh (ControlMaster sockets)

   class SshTransport:  # implements CommandTransport
       def __init__(self, ep: SshEndpoint, user: str, host: str, log: DeviceLog, *,
                    sudo: bool = False, runner=subprocess.run) -> None
       def run(self, cmd, *, stdin=None, timeout=30.0) -> CommandResult
       def copy_out(self, src: str, dest: str) -> None   # scp through the same jump
   ```
2. `argv()` builds: `ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5
   -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=<known_hosts>
   -o ControlMaster=auto -o ControlPath=<control_dir>/%C -o ControlPersist=120
   -J <jump_user>@<jump_host>:<jump_port> <user>@<host> -- <remote>`, where `<remote>` is
   `sh -c <shlex.quote(cmd)>` or `sudo -n sh -c <shlex.quote(cmd)>` (REQ-4.2, REQ-7.3, REQ-7.5).
   The jump hop must use the same known-hosts file (pass it through `-o ProxyCommand`, or set
   both options for the jump with `-J` plus an `ssh_config` written to `control_dir`).
3. Logging and errors copy `DockerExecTransport`: `kind="exec"`, stdout on success and stderr on
   failure, `TimeoutExpired` -> `TransportError`. ssh's own exit code 255 (connection failed)
   becomes `TransportError(f"{host}: ssh failed: {stderr}")`, so polling retries it (REQ-3.5).
4. Tests with an injected `runner` that records argv: the quoting survives a command with
   quotes and `$`; `sudo` wraps only when asked; rc 255 raises; rc 1 returns a result; the
   known-hosts path and `StrictHostKeyChecking=accept-new` are present; no
   `StrictHostKeyChecking=no` anywhere in the module (a test greps the source).

**Verification:**
- [ ] `uv run pytest tests/unit/test_ssh_transport.py -q` passes without running ssh

#### Task C2: `SshTunnel` and `OobAccess`

**Files:** `netcheck/devices/transport/ssh.py`, `tests/unit/test_ssh_transport.py`

1. `SshTunnel(ep, user, host, remote_port=8765, *, popen=subprocess.Popen, connect=socket.create_connection)`:
   `open()` picks a free local port (bind `127.0.0.1:0`, read it, close), starts
   `ssh -N -L 127.0.0.1:<port>:127.0.0.1:<remote_port> ...same options... <user>@<host>`, then
   waits up to 15 s until a TCP connect to `127.0.0.1:<port>` succeeds; returns the port.
   `close()` terminates the process (kill after 5 s). A tunnel whose process exited raises
   `TransportError` with its stderr.
2. `OobAccess(ep, log_for)` owns one `SshTransport` per (device, user) and one `SshTunnel` per
   NVUE device; `close_all()` closes every tunnel; registered with `atexit` so an aborted run does
   not leave ssh processes behind.
3. Tests with injected `popen`/`connect`: the forward spec is local-only (`127.0.0.1:`), a dead
   process raises with its stderr, `close_all` terminates every tunnel once.

**Verification:**
- [ ] unit tests pass; `ps` shows no ssh left after the test session (fakes only, nothing started)

### Track D: NVUE device side

#### Task D1: `NvueSnapshot`

**Files:** `netcheck/backends/nvidia_air.py`, `tests/unit/test_nvue_snapshot.py` (new),
`tests/unit/fakes_nvue.py`

1. ```python
   VOLATILE_KEYS: tuple[tuple[str, ...], ...] = ()   # [S3] paths that differ between two reads

   class NvueSnapshot(_NvueBase, Snapshot):
       def __init__(self, t, port_map, device: str) -> None
       def take(self) -> dict[str, Any]:              # GET "" rev=applied, volatile keys removed
       def restore_to(self, state: dict[str, Any]) -> None:
           if self.take() == state: return
           if "eth0" not in state.get("interface", {}):   # management survives or we stop
               raise TransportError(f"{self.device}: snapshot has no eth0; refusing to replace")
           self.apply([("DELETE", "", None), ("PATCH", "", state)])
           after = self.take()
           if after != state:
               raise TransportError(f"{self.device}: config still differs after restore: {_diff_keys(state, after)}")
   ```
   `_diff_keys` lists the top-level and second-level keys that differ (short, for the report).
   Confirm `GET ""` maps to `/nvue_v1/` in `RestTransport` (`path.lstrip("/")` gives the base).
2. `FakeNvueTransport` learns the root path: `GET ""` with `rev=applied` serves
   `<device>/applied.json` (captured in phase 2; until then build it in the test from the existing
   samples); an applied root `PATCH` in a revision replaces what later `GET ""` returns.
3. Tests: equal state applies nothing (no `POST revision`); a changed link is restored in one
   revision with DELETE then PATCH of the root; a state without `eth0` is refused before any
   call; a fake that keeps returning the old config raises "still differs"; the snapshot passes
   through `json.dumps` (the backend stores it as JSON).

**Verification:**
- [ ] new tests pass; `tests/unit/test_snapshot.py` still passes

#### Task D2: LSDB prefixes and the RIB shape

**Files:** `netcheck/backends/nvidia_air.py`, `tests/unit/test_nvue.py`

1. Replace `NvueOspf.lsdb_prefixes`'s `NotImplementedError` with a GET of the LSDB path
   **[S3]** and a strict parser `parse_nvue_lsdb(data) -> set[str]` (raise `NvueShapeError` on
   unknown shapes, like the others).
2. If S3 shows the pinned release uses the 5.11+ RIB shape, extend `parse_nvue_routes` for it,
   keeping the old shape; add both samples to the parametrized shape tests (REQ-4.4).

**Verification:**
- [ ] `test_nvue.py` passes with a test per new shape; the "unknown shape fails loudly" test
      covers the LSDB parser

#### Task D3: `NvidiaFactory` with hosts and the switch shell

**Files:** `netcheck/backends/nvidia_air.py`, `tests/unit/fakes_nvue.py`, `tests/unit/test_nvue.py`

1. Rename `NvueFactory` to `NvidiaFactory` (keep `NvueFactory = NvidiaFactory` until the tests
   move). Constructor takes `open_api(spec) -> ApiTransport` and `open_shell(spec) -> CommandTransport`.
2. Routers and switches: Links/Ospf/Bridge as today, `snapshot()` -> `NvueSnapshot`.
   Switches also get `shell()` -> `LinuxShell(open_shell(spec), spec.ports, spec.name)`.
   Hosts: `links()` -> `LinuxLinks`, `shell()` -> `LinuxShell`, `snapshot()` ->
   `LocalSnapshot(name, links, host=shell)`, all over `open_shell(spec)` (reused adapters, no copy).
3. `use_nvue_devices(lab)` in `fakes_nvue.py` passes `ScriptedTransport`s for hosts with the
   replies the local fakes already use (see `tests/unit/fakes_local.py`).
4. Update `test_lab_tests_pass_unchanged_on_nvue_responses`: host tests are no longer skipped on
   the NVIDIA factory; assert no skip reason mentions a missing capability.

**Verification:**
- [ ] `uv run pytest tests/unit -q` passes; the pytester test reports zero capability skips

### Track E: settings and the record file

#### Task E1: `backends.yaml` and the simulation record

**Files:** `backends.yaml`, `netcheck/backends/nvidia_air.py` (a small `AirRecord` class),
`tests/unit/test_nvidia_backend.py` (new)

1. `backends.yaml` `nvidia:` gains (values from S1/S2):
   ```yaml
   images:
     router: cumulus-vx-<pinned>     # [S1] exact Air image name
     switch: cumulus-vx-<pinned>
     host: generic/ubuntu2204
   host_port_prefix: eth
   ssh_public_key: ~/.ssh/id_ed25519.pub   # private key from the ssh agent, never read
   jump_user: ubuntu
   simulation_ttl_hours: 4
   sudo: true
   ```
2. `AirRecord(root)`: `.netcheck/nvidia/simulation.json` with `id`, `ssh_fqdn`, `ssh_port`,
   `intent`, `created`; `load() -> dict | None`, `save(...)`, `forget()` (also deletes
   `known_hosts` and the control sockets). File mode `0o600`.
3. Tests: save/load round trip, forget removes all three, a corrupt file loads as None with a
   warning printed (the next deploy then replaces it).

**Verification:**
- [ ] tests pass; `.netcheck/` is already in `.gitignore` (check it)

---

## Phase 2: the backend lifecycle (sequential, after phase 1)

#### Task 2.1: `render`, `is_running`, `devices`, `open_*`

**File:** `netcheck/backends/nvidia_air.py`

1. `render()` -> `render_air(self.intent, self.settings, self.root / "build" / self.intent.name / "nvidia")`.
2. `is_running()`: no record -> False; record -> `AirApi.simulation(id)`; None -> False (the
   simulation was deleted elsewhere: forget the record); otherwise `state == ACTIVE`. A missing
   token raises (REQ-2.7); an `AirError` propagates.
3. `devices()` assembles with `{"router": "swp", "switch": "swp", "host": host_port_prefix}` and
   `NvidiaFactory(self._open_api, self._open_shell)`; `_open_api` opens the device's tunnel via
   `OobAccess` and returns `NvueRestTransport("127.0.0.1", user, password, spec.log, port=<tunnel>)`;
   `_open_shell` returns `SshTransport(ep, "cumulus" | jump_user-for-hosts, spec.name, spec.log, sudo=...)`.
4. `environment()` adds `air_simulation`, the pinned images and the NVUE `system` version
   (`GET system` -> `build`/`version` field **[S3]**), "unreachable" on `TransportError`.

**Verification:**
- [ ] unit test with `FakeAirTransport`: no record -> not running; ACTIVE -> running; deleted
      simulation -> not running and record forgotten

#### Task 2.2: `deploy` and `destroy`

**File:** `netcheck/backends/nvidia_air.py`, `tests/unit/test_nvidia_backend.py`

1. `deploy()` in this order, each step printing one line (`air: importing campus-mini ...`):
   1. `_require_secrets()` names the missing variable (REQ-8.2); run before any call.
   2. If a record exists, `destroy()` it (a deploy replaces; `up()` already decided).
   3. `render()`; import with name `netcheck-<intent>-<YYYYmmdd-HHMM>`; `set_expiry(now + ttl)` (REQ-7.4).
   4. `wait_for_state(INACTIVE)`, `start`, `wait_for_state(ACTIVE)` with the `device_ready`
      timeout from `timing.yaml` (load it with `Timing.load(..., "nvidia", ...)`).
   5. Poll `ssh_service()` until FQDN and port are set; save the record; reset `self._devices`.
   6. Wait until every device answers (`run("true")` over SSH for all; `GET system` over the
      tunnel for routers and switches), polling inside `device_ready`.
   7. Push config: for each router/switch, `apply_changes(t, [("PATCH", "", nvue_*_config(...))])`
      (merge, REQ-5 design note). For each host, run `host_exec(intent, host, prefix)` commands
      (from `netcheck/model/render.py`, reused) through its `SshTransport` with sudo.
2. `destroy()`: no record -> return; `AirApi.delete(id)`; `OobAccess.close_all()`; `AirRecord.forget()`.
3. Tests over `FakeAirTransport`, `FakeNvueTransport` and `ScriptedTransport` (inject the three
   openers): call order import -> expiry -> start -> waits -> config; an INVALID import raises
   with the simulation ID and leaves no record; a missing `NVUE_PASSWORD` raises before the first
   Air call; destroy after a deleted simulation passes; `Backend.up()` with
   `NETCHECK_NO_DEPLOY=1` and no simulation raises `LabDegraded` without an Air import (REQ-2.4).

**Verification:**
- [ ] `uv run pytest tests/unit -q` passes
- [ ] `grep -rn "NotImplementedError" netcheck/backends/nvidia_air.py` finds nothing

#### Task 2.3: CLI and Makefile

**Files:** `netcheck/__main__.py`, `Makefile`, `README.md`

1. The CLI already passes `--backend`; catch `AirError` next to the other lifecycle errors
   (exit 1, `error: ...`).
2. `make lab-up BACKEND=nvidia` works through `$(NETCHECK)`. Add a `nvidia-check` target that
   runs `render` and checks both variables are set, without calling Air. `mutate-lab` stays local.

**Verification:**
- [ ] `make nvidia-check` with the variables unset prints which one is missing and exits non-zero

---

## Phase 3: live runs, samples, timing, CI, docs (needs Air)

#### Task 3.1: first live deploy and captured samples

1. `make lab-up BACKEND=nvidia`; fix what the spikes did not foresee, each fix with a unit test.
2. `record_samples` for r1, r2, sw1 into `tests/unit/fixtures/nvue/<device>/` (and
   `applied.json` from `GET ""`), replacing the doc-based samples; update
   `tests/unit/fixtures/nvue/README.md` with the date, release and command used (REQ-4.5).
3. Remove the "assembled from documented fields" sentence from the `nvidia_air.py` docstring.

**Verification:**
- [ ] `make check` passes on the captured samples; the round-trip tests of task B2 still pass

#### Task 3.2: the lab suite on Air

1. `make test-lab BACKEND=nvidia NETCHECK_SLA_MODE=record`; every failure is triaged as a
   backend bug (fix + unit test) or a timing value (task 3.3), never by editing a lab test.
2. `make demo BACKEND=nvidia`; then `make lab-reset BACKEND=nvidia` leaves no fault.

**Verification:**
- [ ] the run passes with zero capability skips; the JUnit file lists `backend=nvidia`

#### Task 3.3: the timing profile

1. Run the suite three times; collect the JUnit properties per check.
2. Set `timing.yaml` `profiles.nvidia` per check: SLA = worst measured x 1.5, rounded up;
   timeout = SLA x 3 (and at least the current local timeout); keep the formulas referencing
   intent timers where the local profile has them. Write the measurements and the rule into
   ADR-0004 (REQ-6.1).

**Verification:**
- [ ] a fourth run passes in `strict` mode or the ADR says why `record` stays (REQ-6.2)

#### Task 3.4: CI workflow

**File:** `.github/workflows/nvidia.yml`

1. Triggers: `workflow_dispatch` and a nightly `schedule`. Job runs only if
   `secrets.NVIDIA_AIR_TOKEN != ''` (check through an env var in an `if:` on the first step and
   skip the rest).
2. Steps: checkout, setup-uv (with the retry pattern of `ci.yml`), `uv sync`, start an
   ssh-agent with `secrets.NVIDIA_SSH_KEY`, write the public key file, `make lab-up BACKEND=nvidia`,
   `make test-lab BACKEND=nvidia`, upload `artifacts/`, and `make lab-down BACKEND=nvidia` under
   `if: always()` (REQ-8.3). `permissions: contents: read` (plus `checks: write` for the JUnit step).
3. `ci.yml` is not touched (REQ-8.4).

**Verification:**
- [ ] a manual dispatch passes; the Air account shows no simulation afterwards

#### Task 3.5: docs

1. `docs/decisions/0007-nvidia-access-and-snapshots.md`: context, options (run tests on the
   oob-mgmt-server / expose NVUE on the mgmt network / SSH jump and tunnels; SDK vs thin client;
   checkpoints vs NVUE revisions), decision, consequences, open questions.
2. ADR-0001, ADR-0003, ADR-0004: answer each NVIDIA open question with what was found, dated.
3. `docs/decisions/README.md` row for 0007; README Status and "Run on NVIDIA Air" section;
   `GLOSSARY.md` entries if the work introduced a domain term (candidate: **simulation**, the
   Air lab instance; avoid "sim" in prose).

**Verification:**
- [ ] `plan-verifier` run against this plan and the requirements reports COMPLETE

---

## Parallel execution

| Track | Tasks | Files owned | Can run with |
| --- | --- | --- | --- |
| A | A1, A2 | `devices/transport/rest.py`, `devices/transport/nvue_rest.py`, `backends/air_api.py`, `fakes_air.py`, `test_air_api.py` | B, C, D, E |
| B | B1, B2, B3 | `devices/assembly.py`, `backends/air_topology.py`, `test_assembly.py`, `test_air_topology.py` | A, C, D, E |
| C | C1, C2 | `devices/transport/ssh.py`, `test_ssh_transport.py` | A, B, D, E |
| D | D1, D2, D3 | `backends/nvidia_air.py` (device side), `fakes_nvue.py`, `test_nvue*.py` | A, B, C |
| E | E1 | `backends.yaml`, `AirRecord` in `nvidia_air.py`, `test_nvidia_backend.py` | A, B, C (after D, both edit `nvidia_air.py`) |

Phase 2 depends on all of phase 1; phase 3 on phase 2 and the spikes. Merge order for phase 1:
A, B, C (independent files), then D, then E.

## Risks

| # | Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- | --- |
| R1 | Root replace revision drops management access | Med | High | Spike S3 first; `eth0` guard in `NvueSnapshot`; first push merges only |
| R2 | Air API shapes differ from the docs read | Med | Med | Spike S1 with the `parse` endpoint; shapes isolated in `air_topology` and `AirApi` |
| R3 | Volatile keys make every restore "differ" | Med | Med | S3 lists them; `VOLATILE_KEYS` strips them in `take()` |
| R4 | Images lack sudo, tcpdump or fast ping | Low | Med | S2 checks; first-boot instructions install or configure |
| R5 | Tunnels die mid-run | Med | Med | `TransportError` on dead process, polling retries, ServerAlive, ControlPersist |
| R6 | Secrets leak into logs or artifacts | Low | High | redaction through `RestTransport`, placeholder in rendered files, grep in acceptance |
| R7 | Forgotten simulations run up cost | Med | Med | expiry at import, `lab-down` under `always()` in CI, `destroy` on redeploy |
| R8 | Air latency makes the suite slow | High | Low | measured timing profile; `record` SLA mode |

## Completion checklist

- [ ] Every REQ in the requirements maps to a done task (plan-verifier COMPLETE)
- [ ] `make check` green; no test needs Air
- [ ] Live run on Air passes; demo works; no simulation left behind
- [ ] ADR-0007 written; ADR-0001/0003/0004 updated; README and glossary updated
