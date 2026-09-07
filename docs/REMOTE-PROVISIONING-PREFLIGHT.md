# Remote provisioning preflight (inspect/plan)

This slice is **read-only inspect/plan**. It turns a caller-supplied Linux
candidate document into a deterministic plan. It does **not** SSH, probe a
live host, extract an archive, edit shell rc files, inventory secrets or the
filesystem, or apply an installer.

Live remote verification is a later packet. A successful document is always:

- `mode`: `plan_only`
- `verification`: `pending_live_probe`
- `provenance.facts` / `provenance.manifest`: `supplied`
- `provenance.observed`: `unverified`

User-supplied facts must never be reported as observed remote verification.

## Entry points

- Library: `desktop/python-webview-shell/remote_provision_plan.py`
  (`plan_remote_provision`). The public entry accepts **UTF-8 JSON bytes or
  `str` only**, at most 8192 bytes. Python `dict` / `Mapping` objects are not
  an input path.
- Local JSON CLI: `scripts/remote_provision_plan.py` (stdin → stdout, no
  network). Stdin is read as at most 8193 bytes (`MAX_DOCUMENT_BYTES+1`).
  Exit `0` for a plan document, exit `2` for a bounded `PlanError` JSON
  object on stderr (`code` + `detail`, no request echo).

Existing startup, profile, SSOT, and remote-command modules are unchanged.
Product identity comes from `workstack.__version__` and
`REMOTE_PROTOCOL_VERSION`. Path grammar is `validated_posix_path`.

## Request

One JSON object, at most 8192 bytes, exact keys only, duplicate keys refused.

| Field | Meaning |
| --- | --- |
| `schema_version` | Must be `1` |
| `target` | Explicit per-user Linux install: `os`, `install_root`, `data_root`, `owner`, `expected_workspace_id` |
| `artifact` | Manifest identity: `product_version`, `protocol_version`, `digest` |
| `facts` | Candidate Linux facts: `os`, `python`, `install`, `data` |

`target.os` and `facts.os` must be `linux`. Paths are absolute POSIX paths
using the existing remote argv grammar (no `;`, `$(`, `..`, trailing slash,
or Windows paths). `install_root` and `data_root` must be distinct and must
not prefix one another.

`artifact.product_version` must equal this Work Stack version. `artifact.protocol_version`
must equal `REMOTE_PROTOCOL_VERSION`. `digest` is `sha256:` plus 64 lowercase
hex characters — the same shape the Store already uses.

`facts.python` is `null` or `{path, version}`. Compatibility is derived; a
caller `compatible` flag is an unknown key. Missing Python is a
`REMOTE_PYTHON_REQUIRED` prerequisite. The Linux floor is the product
service/CLI contract (`Python 3.10 or newer` in `README.md`), not the
Windows embeddable 3.12.10 bundle. Older than 3.10 is
`REMOTE_PYTHON_TOO_OLD`. There is no parallel SSH/auth schema in this slice.

`facts.install` / `facts.data` are bounded existence summaries, not directory
listings. When `exists` is false, identity fields are null and `symlink` is
false. When `exists` is true, `owner` is required. An existing data path
with `workspace_id` null is `DATA_STATE_UNKNOWN` and is refused.

## Decision

| `decision` | `plan.action` | When |
| --- | --- | --- |
| `prerequisite_missing` | `none` | Compatible Linux Python was not supplied or is older than the 3.10 service/CLI floor |
| `refused` | `none` | Artifact identity is not this product, symlink/reparse, ownership mismatch, workspace mismatch, existing data with unknown workspace identity, or an existing install with unknown or conflicting identity (repair evaluation required; never ordinary install) |
| `current` | `noop` | Supplied app identity matches the artifact (still unverified) |
| `install_needed` | `install` | No app is present at the install root |

The plan names `install_root`, `data_root`, the supplied Python path when
present, and the artifact identity. It never includes a shell snippet.

## What this slice does not do

- SSH, OpenSSH config edits, or live probes
- Installer apply, archive extraction, or any write
- Secret, environment, or filesystem inventories
- Credential handling or invented identity schemas
- Claiming that supplied facts were observed on a remote host
