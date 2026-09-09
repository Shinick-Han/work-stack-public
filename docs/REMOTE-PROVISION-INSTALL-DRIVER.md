# Remote provision-install driver — usage and limitations

`desktop/python-webview-shell/remote_provision_driver.py` (library) and
`scripts/remote_provision_install.py` (CLI) assemble parts that already
existed into one callable operation. They add no new remote protocol, no new
transport, and no new receipt format.

## What it is made of

| Step | Owned by | What the driver does |
|---|---|---|
| Collect facts | `remote_provision_probe.run_remote_provision_probe` | Runs it read-only against the exact selected profile |
| Plan | `remote_provision_plan.plan_remote_provision` | Feeds it the target derived from that profile and the facts just collected |
| Build the payload | `remote_provision_payload.build_installer_payload_source` | Passes the explicitly selected archive and sidecar |
| Build the argv | `remote_provision_command.build_ssh_provision_install_command` | Uses it unchanged; writes the payload to stdin |
| Admit the artifact | `remote_provision_installer*.py` (remote) | Nothing — the installer engine remains the sole admission authority |

Two local helpers and a shared process primitive support the driver:
`remote_provision_evidence.py` (frozen inspection evidence and its digest),
`remote_provision_artifact.py` (binding one archive to one sidecar, and the
strict JSON reader), and `workstack/bounded_process_exchange.py` (one bounded
stdin/stdout exchange with one child process, and honest cleanup reporting).
The desktop `bounded_process_exchange.py` is a compatibility alias to that
shared module; the existing driver call sites retain the same behavior.

## Usage

Inspect and plan (the default; nothing is installed):

```
python scripts/remote_provision_install.py \
  --ssh-host-alias <alias> \
  --ssh-executable <path to ssh> \
  --remote-python /path/to/python \
  --install-root /path/to/new/app/dir \
  --data-root /path/to/existing/data/dir \
  --owner <posix user> \
  --expected-workspace-uid <uuid> \
  --archive WorkStack-Linux-<version>-cp312-manylinux_2_17_x86_64.zip \
  --sidecar WorkStack-Linux-<version>-cp312-manylinux_2_17_x86_64.json \
  --expect-digest sha256:<hex of the archive you mean to install>
```

Install the plan it produced — add `--apply` to the same command.

Exit codes: `0` plan produced or install completed with an exact matching
receipt; `2` invalid input, refused plan, or a clean remote refusal; `3` the
outcome is unknown and must be reconciled by hand.

## Rules it enforces

- **Facts are always live and always bound to the selected profile.** The
  driver builds the planner's `target` block from the profile itself, and
  plans only against facts collected in the same call. A hand-written plan or
  facts document is not accepted anywhere.
- **Inspect/plan is the default.** `apply_remote_install` refuses unless the
  caller passes `apply=True` (`--apply` on the CLI) *and* the freshly
  recomputed plan decides `install_needed`.
- **The install is authorised by a second reading, not by the plan you were
  shown.** `apply_remote_install` re-runs the read-only probe against the same
  profile and re-plans immediately before it builds an install command. A
  target that became occupied, changed workspace, or merely changed since the
  inspection refuses (`DRIVER_PLAN_REFUSED`, `DRIVER_TARGET_CHANGED`), and a
  target that cannot be read again refuses too (`DRIVER_REVALIDATION_FAILED`).
  Not being able to verify is never an implicit approval.
- **A retained inspection is a record, not a licence.** `facts` and `plan` are
  deep-frozen read-only mappings, and an inspection carries a digest over the
  binding and both documents. An edited document refuses with
  `DRIVER_EVIDENCE_MISMATCH`, a hand-built one with `DRIVER_EVIDENCE_UNISSUED`,
  and a malformed one with `DRIVER_EVIDENCE_INVALID` — each before any process
  is created. That digest is integrity inside this API. It is not
  authentication: Python running as the same user can reach whatever this
  process can reach, and no secret, signature, or distributed proof is claimed.
- **The binding is checked again at apply time.** An inspection carries a
  digest over the target, owner, and artifact; editing the profile or swapping
  the artifact after the plan refuses with `DRIVER_BINDING_MISMATCH` before any
  process is created.
- **Fresh directory, expected workspace, only.** The install root must not
  exist, and the data root must already exist and already carry the expected
  workspace UUID. This is checked against both the retained and the re-read
  facts, so a doomed attempt costs no install work.
- **`StrictHostKeyChecking=yes` and `BatchMode=yes` stay on**, `shell=False`,
  a fixed argv shape, bounded stdin/stdout/stderr and timeouts, and the child
  process is killed and reaped on any timeout or oversize. The driver only
  ever stops a process it created itself.
- **Cleanup that did not work is said out loud.** Every report carries
  `cleanup: "settled"` or `cleanup: "unsettled"`. A refused kill, a child that
  never reaps, or an I/O thread still alive after its bounded join makes the
  outcome `unsettled` and adds guidance to check for a stray local client
  process. It never downgrades an unknown commit to a refusal and never
  suggests a retry.
- **No host is chosen for you.** Every target field is required on argv. The
  CLI reads no SSH config, no saved profile, and no SSOT.
- **Unknown stays unknown.** A timeout, an oversized response, an
  unrecognisable success line, or `REMOTE_INSTALL_COMMIT_UNKNOWN` reports
  `outcome: "unknown"` with reconciliation guidance. Nothing is retried,
  overwritten, deleted, or stolen — there is no flag that does any of those.
- **Bounded errors.** Remote stderr is admitted only as one of a fixed set of
  installer codes; anything else collapses to `REMOTE_INSTALL_FAILED`. No
  remote text, host name, or credential reaches a message or a report.
- **An install is not an activation.** Every report carries
  `activation: "not_activated"`. The driver never touches the connection
  registry, the SSOT, an owner receipt, or a running server.

## Limitations — what this does not do

1. **It does not switch the active profile.** Installing an application
   directory changes nothing about which one is served. The next step is the
   existing protocol, driven separately: stop the *self-owned* old server
   (`stop-owned`, which needs the session token of the desktop that started
   it — if this desktop does not hold that token, stop and report, never force),
   then Test → `issue_successful_test_proof` → `activate` → reconnect →
   `confirm`, or `restore` on failure. The old application directory is not
   deleted before `confirm`.
2. **It does not check the store schema or take a backup.** A new application
   opening a data root at `store_schema_version < 5` triggers an automatic,
   unconditional upgrade. Read `/api/v1/storage` and take an explicit
   `workstack maintenance backup --out …` *before* activation. That gate is not
   in this packet.
3. **It does not implement automatic profile switching**, and deliberately
   avoids anything that would create a second writer or migrate the SSOT.
4. **It does not verify that `frontend/dist` matches the source commit.** That
   gate belongs to the artifact builder, not here.
5. **The installer engine's pinned release constants are a prerequisite.**
   `remote_provision_installer*.py` verify the artifact against their own
   `PRODUCT`/`PROTOCOL` constants, and the planner separately requires
   `artifact.product_version == workstack.__version__`. Until those agree, no
   artifact satisfies both gates and the driver will faithfully report the
   refusal. Repairing them is owned elsewhere; this driver does not paper over
   it.
6. **It is a prerequisite, not the user-facing fix.** There is no desktop UI
   for it yet. The library is deliberately importable so a native surface can
   call it later without going through the CLI.

## Tests

`tests/test_remote_provision_driver.py`. Every input is synthetic and no real
SSH runs, nothing is installed, and no user directory, company host, or store
is touched. Most tests use fake in-process remote legs; a few run a local
Python child in place of `ssh` so the real pipe, timeout, and cleanup boundary
is exercised without a network.
