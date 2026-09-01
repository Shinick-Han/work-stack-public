# Work Stack SSOT SSH / WSL validation receipt

Date: 2026-09-01  
Environment: Windows host + WSL2 `Ubuntu` + isolated OpenSSH server

## Scope

This canary exercised the production SSH command builders and connection
monitor against a disposable Linux authority. It did not use or modify the
configured Work Stack SSOT.

The canary created:

- a unique `/tmp/workstack-wsl-ssh-canary-<uuid>` authority and runtime;
- an ephemeral host key, client key, host alias, SSH port, forward port, and
  remote server port;
- a temporary OpenSSH config entry that was restored byte-for-byte on exit.

The reusable entry point is:

```powershell
python scripts/windows/test_workstack_wsl_ssh_canary.py --distro Ubuntu
```

The release-oriented wrapper first runs the deterministic matrix and can then
run the WSL canary:

```powershell
python scripts/run_ssh_regression.py --wsl-distro Ubuntu
```

## Defects found and corrected

1. The pinned `rpds-py==2026.6.3` dependency omitted the CPython 3.14
   manylinux wheel hash. Linux installation correctly failed closed. The
   observed PyPI wheel hash was added to `requirements.txt`.
2. The remote server rejected a valid SSH-forwarded request whenever the
   local forward port differed from the remote listening port. The SSH command
   now passes an explicit `--public-port`; the HTTP boundary accepts that
   additional port only for a loopback host, while preserving same-origin and
   CSRF checks.
3. Abruptly terminating Windows OpenSSH orphaned the remote Python server. The
   orphan retained the writer lease, so automatic reconnect could not recover.
   SSH-owned servers now use Linux `PR_SET_PDEATHSIG` through the explicit
   `--exit-with-parent` flag. Local non-SSH servers are unchanged.

## Passed evidence

Final evidence file:

`.artifacts/wsl-ssh-canary/a796db51fd424249a3f1224660909cb4/evidence.json`

The final run proved:

- WSL prerequisites and isolated SSOT initialization;
- the product's strict read-only remote profile check;
- loopback-only SSH port forwarding and `/api/v1/health` readiness;
- exact remote workspace identity verification;
- a real Task write through the forwarded HTTP API;
- forced SSH process loss followed by monitor state transition
  `ready -> reconnecting -> ready`;
- the same Task remained readable after reconnect;
- the authoritative SSOT digest was byte-identical across reconnect:
  `94681d78cc24d9cc8f70356429e4a58524159bfe6e831cd50b9493c56ef778d0`;
- a concurrent second server was refused with exit code 2 and
  `the Work Stack data directory is already owned by another writer`.

Regression result after the fixes:

- Python: 887 passed, 4 skipped.
- Storage regression matrix: 315 passed, 3 skipped.
- Deterministic SSH regression matrix: 54 passed.
- WSL/OpenSSH network canary: passed, including forced reconnect and
  second-writer refusal.
- Frontend: 69 files and 343 tests passed with coverage collection enabled.
- Structural quality gate: 164 production files passed.
- Export audit: 585 UTF-8 source-policy files passed.
- `git diff --check`: passed (line-ending conversion warnings only).

## Cleanup and host impact

- The temporary WSL authority, server, sshd, client private key, and host alias
  were removed.
- No `%USERPROFILE%\.ssh\config` existed before the canary; it was absent again
  after the canary.
- `openssh-server`, `python3-venv`, `build-essential`, and `python3-dev` were
  installed in WSL to exercise Ubuntu's Python 3.14 source-build path.
- The package-created global `ssh.socket` was disabled and stopped after the
  test. No persistent port 22 listener remains.

## Remaining external validation

WSL proves the product protocol, process lifetime, writer exclusion, and data
preservation on a real Linux/OpenSSH boundary. It does not reproduce a company
bastion, ProxyJump, enterprise host-key rotation, WAN packet loss, sleep/VPN
transitions, or high-latency reconnect behavior. Those remain a separate
real-network canary and should not block the local implementation evidence.

On a machine with an activated real Remote SSH profile, run the following
read-only diagnostic. It uses the saved OpenSSH alias, so ProxyJump/bastion and
VPN routing are exercised without storing a password, key, hostname, user, or
ProxyCommand in the receipt:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\Test-WorkStackRemoteNetwork.ps1
```

The diagnostic validates the installed product profile and records bounded SSH
round-trip samples under `%LOCALAPPDATA%\WorkStack\diagnostics` by default. A
VPN sleep/resume or forced network-loss test remains manual because deliberately
disconnecting a user's real tunnel is outside a read-only release gate.

The structural quality baseline was refreshed only after confirming that the
configuration change introduced no unclassified files, architecture
violations, dependency cycles, or critical-complexity regressions. The final
gate passes across 164 production files.
