---
name: network-security
description: "Security rules for netcheck, a lab automation tool holding cloud and device credentials: secrets, device access (SSH, NVUE TLS), command injection, log and artifact redaction, CI secrets, supply chain, lab blast radius. Use when reviewing or writing code that handles credentials, opens connections, builds shell commands or API paths, writes logs/artifacts, or changes CI workflows."
---

# Security in netcheck

netcheck drives lab networks, not production ones, but it holds real credentials (an NGC
API key that can create billed DSX Air simulations, device passwords, SSH keys) and runs
commands as root inside containers and simulations. Its artifacts are published by CI.
For per-change checklists see [checklists.md](checklists.md).

## Trust boundaries

| Source | Trust | Why |
| --- | --- | --- |
| `intent/lab.yaml`, `backends.yaml`, `timing.yaml` | repo-controlled, reviewed | still validated: they are interpolated into commands and API paths |
| Environment (`NVIDIA_AIR_TOKEN`, `NVUE_PASSWORD`, SSH agent) | secret | must never be echoed anywhere |
| Device and Air API responses | untrusted data | parsed strictly; never executed, never used as a path or command without validation |
| `.netcheck/` state files | local, writable by the user | read defensively (a corrupt file is "none", not a crash or a command) |
| CI pull requests from forks | untrusted code | never get secrets |

Golden rule: **can this value reach a shell, a URL path, a log line or an artifact, and who
controls it?**

## Confidence before reporting

| Confidence | Criteria | Action |
| --- | --- | --- |
| HIGH | the flow is traced from source to sink, and the source is attacker- or device-controlled, or a secret reaches a sink | report with file, line, scenario, fix |
| MEDIUM | the pattern is risky, the source is unclear | note for manual check |
| LOW | best-practice deviation | mention only if asked |

Not findings: fakes and fixtures under `tests/`, values from the reviewed config files that
are validated, `verify_tls: false` when it is set explicitly and the run says so.

## Rules

### Secrets — CRITICAL
- Credentials come from the environment or the SSH agent only. Never in code, the intent,
  `backends.yaml`, rendered files under `build/`, `.netcheck/`, logs, reports, JUnit XML or
  `artifacts/`. Private keys are never read by netcheck; `ssh` uses the agent.
- Every API body logged goes through `redact()` (`SECRET_WORDS`); headers are not logged.
  A new secret-bearing field is named so `redact` catches it, or `SECRET_WORDS` grows.
- Exceptions and failure messages never include a token or password (check f-strings that
  format a request, a URL with credentials, or `self.__dict__`).
- Secret-holding state files are mode `0o600`.

### Device access — HIGH
- TLS to NVUE is verified by default (`verify_tls: true` or `ca_bundle`). Turning it off is an
  explicit setting for a throwaway lab, shown in the run header.
- SSH: `BatchMode=yes`, host keys pinned per simulation (`StrictHostKeyChecking=accept-new`
  with a dedicated `UserKnownHostsFile`). `StrictHostKeyChecking=no` and
  `UserKnownHostsFile=/dev/null` are findings.
- Tunnels bind `127.0.0.1` only (`-L 127.0.0.1:<port>:...`), never `0.0.0.0` or a bare port.
- Every connection and call has a timeout (a hang is a denial of the whole CI job).

### Injection — CRITICAL
- Shell commands: every interpolated value is validated (logical port via `PortMap`, IP via
  `ipaddress`, VLAN as `int`) and `shlex.quote`d when it is free text (BPF filters, paths).
  Remote commands over SSH are quoted once more for the remote shell
  (`sh -c <shlex.quote(cmd)>`).
- `subprocess.run` gets an argv list; `shell=True` is a finding.
- API paths: segments come from `PortMap` or constants, never from a device response.
- YAML is loaded with `yaml.safe_load`; JSON from devices with `json.loads` only.

### Lab blast radius — HIGH
- The framework touches only what it created: containers labelled with the lab name, the
  Air simulation recorded in `.netcheck/nvidia/`. Destroy and cleanup never use wildcards
  over the account or host.
- Every Air simulation gets an expiry at creation; CI deletes it under `if: always()`.
- A restore never removes the management path to a device (guard before applying).

### CI and supply chain — HIGH
- Secrets are used only in workflows that do not run on `pull_request` from forks
  (`workflow_dispatch`, `schedule`, `push` to the main branch); jobs declare minimal
  `permissions`.
- Downloads are checked: `curl --fail`, a pinned version, and a checksum
  (`scripts/install-clab.sh` is the pattern). Container images are pinned by tag in
  `backends.yaml` and by digest where `make pin-images` has been run.
- New Python dependencies are justified (the project keeps three runtime deps), pinned
  through `uv.lock`, and from well-known publishers.
- Actions are pinned to a major version at least; third-party actions reviewed before use.

## Secret patterns to scan for

| Type | Pattern |
| --- | --- |
| NGC / NVIDIA API key | `nvapi-[A-Za-z0-9_-]{20,}` |
| Generic assignment | `(token\|password\|secret\|api_key)\s*[:=]\s*['"][^'"$]{8,}` |
| Private key | `-----BEGIN [A-Z ]*PRIVATE KEY-----` |
| GitHub token | `gh[pousr]_[A-Za-z0-9]{36,}` |
| Basic auth in URL | `https?://[^/\s:]+:[^@\s]+@` |

Never committed: `.env*`, `.netcheck/`, `build/`, `artifacts/`, `mutants/`, `*.pcap`.

## Severity

| Severity | Examples here |
| --- | --- |
| CRITICAL | token or password in a committed file or CI artifact; command injection from a device response; secrets exposed to fork PRs |
| HIGH | TLS or host-key checking disabled by default; tunnel bound to all interfaces; destroy that can hit resources the run did not create; no expiry on simulations |
| MEDIUM | missing timeout; secret-looking field not covered by `redact`; unpinned download without checksum |
| LOW | state file permissions; verbose error that names a username |
