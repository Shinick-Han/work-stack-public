# Work Stack

Work Stack is a local-first, goal-aligned personal execution system. It keeps
objectives, planning tasks, work logs, notes, and sanitized external context in one
workspace and presents the same task model through Graph, Board, and Treemap views.
The Context Inbox lets a user review a sanitized capture, link it to an existing task,
turn an action item into a new task, or create a task directly from the source.

```text
objectives → planning tasks → daily records → weekly roll-up
                 ↕ notes and sanitized captures
```

## Download the Windows installer

Download the immutable release asset (recommended):

- [`WorkStack-Setup-1.0.1.ps1`](https://github.com/Shinick-Han/work-stack-public/releases/download/v1.0.1/WorkStack-Setup-1.0.1.ps1)
- [`WorkStack-Setup-1.0.1.ps1.sha256`](https://github.com/Shinick-Han/work-stack-public/releases/download/v1.0.1/WorkStack-Setup-1.0.1.ps1.sha256)

The same self-contained installer is also committed with this public snapshot:

- [`installer/WorkStack-Setup-1.0.1.ps1`](installer/WorkStack-Setup-1.0.1.ps1)
- [`installer/WorkStack-Setup-1.0.1.ps1.sha256`](installer/WorkStack-Setup-1.0.1.ps1.sha256)

Verify the adjacent SHA-256 sidecar before running the installer. The target machine does not
need a separate Python or Node.js installation. This build is not code-signed, so the checksum
proves transfer integrity rather than publisher identity.

This prototype is intentionally a thin local product shell. Activepieces and the
Conduit runtime are not embedded. Work Stack owns `PlanningTask`; a future Conduit
integration owns execution tasks. See the
[Work Stack / Conduit boundary ADR](docs/WORKSTACK-CONDUIT-TASK-BOUNDARY-ADR.md).

## Current release mode

The current release label is **manual sanitized capture prototype**. A user or trusted
adapter must remove message bodies, headers, addresses, quoted replies, HTML, attachment
content, and other raw source material before sending a Capture Packet to Work Stack.
Work Stack stores only the validated, normalized packet projection.

`oob_verified` exists in the contract and synthetic fixture, but it is not a claim that
the Microsoft 365 path has been verified in this environment. Before using any
Microsoft 365 read-only or bidirectional dogfood label, run the relevant provider gates
with non-sensitive source material and retain the evidence described in
[the release checklist](docs/RELEASE-CHECKLIST.md). This includes checking tool scope,
provenance, canary removal, immutable identifiers, approval behavior, and upstream
retention separately.

The product also contains a user-mediated Outlook/Teams OOB handoff. It does not claim a
direct background connection: Work Stack prepares a bounded request, the user runs it
in an already authenticated agent, and Work Stack imports the sanitized result. Real
Outlook and Teams read/reply labels remain gated separately in the release checklist.

The fixture-backed Outlook/Teams implementation is complete, but no real Microsoft
provider capability has passed Gate 0 in this environment. The default production build
therefore disables all four Microsoft lanes:

| Provider | Read Gate 0 | Read build flag | Reply Gate 0 | Reply build flag | Default UI |
| --- | --- | --- | --- | --- | --- |
| Outlook | Unverified | `VITE_WORKSTACK_OUTLOOK_READ_VERIFIED` | Unverified | `VITE_WORKSTACK_OUTLOOK_REPLY_VERIFIED` | Read and reply disabled |
| Teams | Unverified | `VITE_WORKSTACK_TEAMS_READ_VERIFIED` | Unverified | `VITE_WORKSTACK_TEAMS_REPLY_VERIFIED` | Read and reply disabled |

Each flag is enabled only when its trimmed, case-insensitive build-time value is exactly
`true`; an unset value, empty string, `1`, `yes`, or any other value is disabled. A
provider's reply UI also requires its read flag, so a reply flag cannot enable a
provider that has not passed its read gate. These flags are release evidence gates, not
runtime connection or health indicators. The generic manual Capture import remains
available regardless of their values.

## Prerequisites

- Python 3.10 or newer for the local service and CLI
- Node.js `^20.19.0` or `>=22.12.0` and npm for the React build

Snapshot export pins Unicode Standard 17.0.0 through the hash-locked
`unicodedata2==17.0.0` dependency. The React workspace has npm dependencies locked by
`frontend/package-lock.json`.

## Windows one-file setup

The current prototype installer bundles the official 64-bit Python 3.12.10 embeddable
runtime, the hash-locked Unicode wheel, and a pinned pywebview/pythonnet desktop host.
The Start-menu shortcut runs the PSF-signed `pythonw.exe`; the Work Stack UI owns the
window and Context Inbox owns the embedded Outlook, Teams, or OneNote viewport. Build the React UI, then create one
self-contained PowerShell setup file:

```powershell
npm --prefix frontend run build
powershell -ExecutionPolicy Bypass -File scripts\windows\Build-WindowsInstaller.ps1
```

Run the generated `.artifacts\WorkStack-Setup-1.0.1.ps1`. The target machine needs neither
Python nor Node.js, and installation does not contact the network. It installs the bundled
runtime under `%LOCALAPPDATA%\Programs\WorkStack`, keeps planning data and versioned backups
under `%LOCALAPPDATA%\WorkStack`, and adds a Start menu shortcut. Re-running a new setup
artifact stops only the matching installed Work Stack process, writes a verified pre-upgrade
backup, and replaces the application while preserving data.

The builder also writes `WorkStack-Setup-1.0.1.ps1.sha256`. Keep it beside the setup artifact
and verify both the filename and digest before execution:

```powershell
.\scripts\windows\Test-WorkStackSetup.ps1 `
  -SetupPath .\.artifacts\WorkStack-Setup-1.0.1.ps1
```

This is transfer-integrity evidence, not publisher authentication. The prototype remains unsigned
until a trusted Windows code-signing certificate is available.

`Uninstall-WorkStack.ps1` removes the application and preserves planning data by default.
Pass `-RemoveData` only when the local data and backups should also be deleted.

`Update-WorkStack.ps1` requires the adjacent checksum sidecar and runs the strict verifier before
it reads the installed configuration or invokes the selected setup artifact.

## Build and run

From this directory:

```powershell
python -m pip install --require-hashes -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
python run_work_stack.py graph serve --host 127.0.0.1 --port 8765 --seed-demo
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). Stop the server with `Ctrl+C`.

`--seed-demo` copies the tracked synthetic fixtures (30 tasks, 5 objectives, and linked
notes/work logs) only when the runtime core is empty. It refuses to overwrite a runtime
that already contains tasks, objectives, work logs, or notes. Omit the flag for normal
use.

The server serves `frontend/dist` when a React build is present. Without it, only the
legacy dependency-free page in `web/index.html` is served.

## Export one planning snapshot to Conduit

Open a task, choose **Export to Conduit**, and review the exact title and detail. The
dialog states that the file is a copy rather than a live link, that importing it does
not update Work Stack, and that Conduit execution still requires confirmation. It also
discloses that objectives, dependencies, subtasks, notes, and tags are omitted. The
save action remains disabled until the disclosure checkbox is selected.

The downloaded name is `<planning-task-uid>.workstack-task.json`. It contains exact
canonical bytes for one committed revision. Preview, cancellation, refusal, and
download leave the Task revision, planning status, Activity, idempotency records, and
journal byte-identical. Work Stack does not contact Conduit; the user carries the file.

The equivalent auditable CLI flow is two-step and refuses overwrite:

```powershell
python run_work_stack.py snapshot preview T-0001
python run_work_stack.py snapshot export T-0001 `
  --out .\<planning-task-uid>.workstack-task.json `
  --expected-revision 3 `
  --expected-digest sha256:<reviewed-digest> `
  --confirm-disclosure
```

Use the revision and digest printed by `snapshot preview`. If the Task changes between
review and export, reopen the preview and review the new revision.

## Runtime data

On Windows, the defaults are:

- data: `%LOCALAPPDATA%\WorkStack\data`
- ephemeral server metadata/token: `%LOCALAPPDATA%\WorkStack\runtime\<data-path-hash>`

When `LOCALAPPDATA` is unavailable, the fallback locations are
`~/.local/share/workstack` and `~/.local/state/workstack`. Runtime state is kept outside
the repository.

Set `WORK_STACK_HOME` to change the data directory for every invocation:

```powershell
$env:WORK_STACK_HOME = 'D:\WorkStackData'
python run_work_stack.py graph serve --seed-demo
```

Alternatively, put `--data-dir` before the command domain:

```powershell
python run_work_stack.py --data-dir .\.runtime\data graph serve --seed-demo
```

The server and every CLI invocation that should share a workspace must resolve to the
same data directory. `WORK_STACK_RUNTIME` may separately override the directory that
holds ephemeral server discovery metadata and the per-process capture token.

## Linux SSOT over SSH

The Windows desktop can use a protected Linux Work Stack store without mounting it and without
running a permanent daemon. While the desktop window is open, it owns one strict OpenSSH session,
starts the Linux server on loopback, and forwards it to Windows loopback. Closing the app ends that
session; Linux remains the only planning-state authority.

First configure a trusted OpenSSH alias in the user's normal SSH config and verify its host key.
Read the existing Work Stack UUID from the Linux SSOT's `workspace.json` (`id` field), then write
the non-secret connection profile:

```powershell
.\scripts\windows\Configure-WorkStackRemote.ps1 `
  -SshHostAlias workstack-linux `
  -RemoteAppDir /srv/workstack/app `
  -RemoteDataDir /srv/workstack/ssot `
  -WorkspaceId 11111111-1111-4111-8111-111111111111 `
  -Check
```

The profile is `%LOCALAPPDATA%\WorkStack\remote-connection.json`. It contains no password, token,
private key, or host-key bypass. The default Windows forward is `127.0.0.1:18765`; the Linux server
remains on `127.0.0.1:8765`. The expected workspace UUID is mandatory, so a typo cannot silently
open a different or newly initialized store. Use `-UseLocal` to return explicitly to the installed
local store.

When a Linux agent needs to change a Task, it should use the revision-guarded command rather than
editing JSON directly:

```bash
printf '%s' '{"workspace_id":"<uuid>","task_id":"T-0001","expected_revision":3,"changes":{"detail":"Reviewed update"}}' | \
  python3 run_work_stack.py --data-dir /srv/workstack/ssot \
    agent apply --stdin --intent-id agent.update.0001
```

If the server is active, the CLI forwards through it; otherwise it acquires the Store lease and
commits locally. Direct SSOT edits are still detected by a persisted manifest. Work Stack pauses all
writes, shows **Review SSOT changes**, and resumes only after the unchanged candidate passes complete
validation and the user explicitly accepts it. Corrupt, partial, cross-workspace, Task-removing, or
non-advancing Task edits stay fail-closed. This first slice does not provide an offline Windows
replica, automatic field-level merging, or restore from a pre-adoption byte backup.

## Send a sanitized capture

Keep the Work Stack server running, then open a second PowerShell window in this
directory. This imports the safe manual fixture through the same loopback API used by an
adapter:

```powershell
Get-Content .\contracts\capture-packet-v1.manual.fixture.json -Raw |
  python run_work_stack.py capture ingest --stdin
```

If the server uses `--data-dir` or `WORK_STACK_HOME`, use the same setting here. The CLI
does not write capture state directly: it discovers the server for that data directory,
reads its short-lived bearer token, and forwards the packet to `POST /api/v1/captures`.
Input is one UTF-8 JSON object and is limited to 64 KiB. An idempotency key is derived
from canonical JSON unless `--idempotency-key` is provided.

The wire format, rejection behavior, provenance modes, and endpoint contracts are in
[contracts/api-v1.md](contracts/api-v1.md).

## Outlook and Teams OOB handoff

No Outlook or Teams desktop application is required. The official OOB connector remains
inside the authenticated agent session; Work Stack never stores its OAuth credentials.
In the default build, the Microsoft controls below are disabled because Gate 0 is still
unverified.

Enable only a capability that has completed the matching real, non-sensitive Gate 0
checks in [the release checklist](docs/RELEASE-CHECKLIST.md), retain that evidence, set
its build flag to `true`, and rebuild the frontend. For example, after Outlook read has
actually passed:

```powershell
$env:VITE_WORKSTACK_OUTLOOK_READ_VERIFIED = 'true'
npm --prefix frontend run build
```

Do not enable an Outlook or Teams reply flag until that provider's read gate and reply
gate have both passed. Leave every unverified flag unset or false. Changing a flag after
the frontend is built has no effect; create a new build.

Read flow:

1. In Context Inbox, choose **Copy Microsoft 365 request**.
2. Select Outlook or Teams, enter the narrow search, and copy the generated request.
3. Run it in the agent session that already has the matching OOB connection.
4. Copy the returned sanitized Capture Packet and choose **Import agent result**.
5. Review the Capture, then link it to a Task or choose **Create task from this source**.

Reply flow:

1. Open a Task with linked Outlook or Teams context and choose **Prepare reply**.
2. Review the fixed source target and plain-text body, then explicitly approve.
3. Copy the approved ReplyCommand to the authenticated agent.
4. The agent replies only to that canonical source target and returns a ReplyReceipt.
5. Import the receipt. `sent`, `failed`, and `unknown` are terminal local states;
   `unknown` is never resent automatically.

The UI does not require hand-editing JSON, does not expose arbitrary recipients, and
does not claim background sync or provider health. The exact envelopes and safety rules
are frozen in [contracts/api-v1.md](contracts/api-v1.md). Synthetic examples are under
`contracts/oob-request-v1.*.fixture.json`, `contracts/reply-command-v1.*.fixture.json`,
and `contracts/reply-receipt-v1.*.fixture.json`.

## Other CLI examples

Run these only while the web server for the same data directory is stopped; the server
holds the sole writer lease for its lifetime.

```powershell
python run_work_stack.py okr add-objective "Improve delivery quality"
python run_work_stack.py backlog add "Create release checklist" --objective O-1
python run_work_stack.py worklog add T-0001 --done "Drafted checklist"
python run_work_stack.py weekly --days 7
python run_work_stack.py graph export --out graph-data.json
```

## Verification

```powershell
python -m unittest discover -s tests -v
npm --prefix frontend test
npm --prefix frontend run build
python scripts/audit_export.py .
```

## Optional QR transfer tools

The SQR1 packaging scripts are an offline, optional repository-transfer aid; they are not part of
the installed Work Stack product or its Docking v1 interface. On 64-bit Windows Python 3.12,
install their independently hash-locked image dependencies with:

```powershell
python -m pip install --require-hashes -r requirements-qr-windows.txt
```

`package_for_qr.py` creates deterministic text frames, `render_qr.py` renders PNG frames and a
contact sheet, and `restore_from_png.py` verifies frame CRCs plus the final archive SHA-256. Frame
and image output directories must be new or empty; the tools refuse to delete existing content.
Both image scripts expose `--help` even when the optional dependencies are not installed.

For a release decision, use [docs/RELEASE-CHECKLIST.md](docs/RELEASE-CHECKLIST.md) and
the threat model in [SECURITY.md](SECURITY.md).

The default audit recognizes a Work Stack source repository and checks its explicit
product-source allowlist while excluding reproducible dependencies, compiler output,
caches, and VCS metadata. To inspect every file in a prepared runtime or export tree,
use the stricter tree policy:

```powershell
python scripts/audit_export.py <runtime-or-export-directory> --mode tree
```

## Repository map

- `workstack/store.py`: atomic JSON persistence, recovery journal, and writer lease
- `workstack/service.py`: shared domain and capture workflow rules
- `workstack/capture.py`: Capture Packet validation and sanitization boundary
- `workstack/cli.py`: CLI commands and server-forwarded capture ingest
- `workstack/server.py`: loopback HTTP server, API, and static UI host
- `frontend/`: React/Vite product UI
- `contracts/`: versioned capture/API contracts and synthetic fixtures
- `data/`: tracked synthetic demo fixtures
- `scripts/`: export audit and reversible packaging tools
- `tests/`: Python domain, recovery, capture, and HTTP boundary tests
