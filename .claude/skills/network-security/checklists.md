# Security checklists by change type

Run the lists that match the files in the diff.

## New or changed transport (`netcheck/devices/transport/`)
- [ ] Every call has a timeout; a timeout raises `TransportError`
- [ ] Every call is logged through `DeviceLog.record` with bodies through `redact()`
- [ ] No header, credential or full URL with userinfo is logged or put in an exception
- [ ] TLS verification on by default; SSH host keys pinned; tunnels on 127.0.0.1
- [ ] `subprocess` gets argv lists; remote commands quoted for the remote shell

## New or changed adapter or parser (`netcheck/backends/`)
- [ ] Interpolated values come from `PortMap`, the intent (validated) or constants
- [ ] Free text in commands is `shlex.quote`d
- [ ] Device responses are only parsed, never used as commands, paths or file names
- [ ] Restore and fault code cannot cut off management access

## Backend lifecycle (`deploy`, `destroy`, state files)
- [ ] Credentials read from the environment at the edge, checked before the first remote call
- [ ] Rendered files contain placeholders, not secrets
- [ ] Destroy targets only the recorded lab or simulation; succeeds when it is already gone
- [ ] Simulations get an expiry at creation
- [ ] State files under `.netcheck/` written with mode 0o600 when they hold access data

## Logs, reports, artifacts (`devices/devicelog.py`, `run/report.py`, `run/testrun.py`)
- [ ] After a run: `grep -rn "$NVIDIA_AIR_TOKEN\|$NVUE_PASSWORD" artifacts/ build/ .netcheck/` finds nothing
- [ ] JUnit `<system-out>` carries the device log only for failed tests (ADR-0005)

## CI workflows (`.github/workflows/`)
- [ ] Secrets only in workflows not triggered by fork pull requests
- [ ] `permissions:` minimal per job
- [ ] Teardown steps run under `if: always()` and do not depend on the failed step's outputs
- [ ] Downloads use `--fail`, a pinned version and a checksum
- [ ] Artifacts uploaded do not include `.netcheck/` or `build/` with secrets

## Dependencies (`pyproject.toml`, `uv.lock`, `backends.yaml`, scripts)
- [ ] New package justified, maintained, well-known publisher; lockfile updated
- [ ] Image and tool versions pinned in one place (`backends.yaml`)
