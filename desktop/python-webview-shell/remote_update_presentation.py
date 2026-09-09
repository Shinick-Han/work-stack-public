"""Snapshot projection and HTML for the remote update page.

Projects the shared ``remote-update-view/1`` snapshot into one status sentence,
one next action, and a collapsed diagnostics block. Host glue and request
admission live in ``remote_update_view``.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

from brand_assets import inline_mark_markup
from native_theme import normalize_theme, theme_color
from remote_update_presentation_actions import (
    ACTION_LABELS,
    ACTIONS,
    MAX_ACTIONS,
    READ_ONLY_ACTIONS,
    REMOTE_ACTIONS,
    THIS_PC_ACTIONS,
    next_admissible_action,
)
from workstack_update_status import canonical_version


SCHEMA_VERSION = "remote-update-view/1"
REMOTE_UPDATE_REQUEST_TYPE = "workstack-remote-update-request"
REMOTE_UPDATE_CAPABILITY_BYTES = 32
_CAPABILITY_LENGTH = REMOTE_UPDATE_CAPABILITY_BYTES * 2

STAGES = frozenset({
    "idle",
    "preview",
    "stop",
    "backup",
    "prepare",
    "probe",
    "activate",
    "verify",
    "ready",
    "failed",
    "cancelled",
    "unknown",
})
OWNER_STATES = frozenset({
    "live",
    "stopping",
    "dead",
    "foreign",
    "unfenced",
    "unknown",
})
EVIDENCE_STATES = frozenset({"verified", "failed", "unknown"})
BACKUP_STATUSES = frozenset({"verified", "failed", "not_run", "unknown"})
INSTALL_CAPABILITIES = frozenset({"available", "unavailable", "unknown"})
INSTALL_METHODS = frozenset({"transactional", "verified_unpack", "unknown"})
CODES = frozenset({
    "unknown",
    "local_only",
    "desktop_current",
    "desktop_available",
    "desktop_blocked",
    "remote_offline",
    "remote_unknown",
    "remote_current",
    "remote_mismatch",
    "remote_lock_owned",
    "owner_live",
    "owner_stopping",
    "owner_dead",
    "owner_foreign",
    "owner_unfenced",
    "token_missing",
    "backup_required",
    "backup_failed",
    "migration_required",
    "nfs_alternative",
    "protocol_below",
    "ready",
    "failed",
    "cancelled",
})
REMOTE_CODES = frozenset({
    "remote_offline",
    "remote_unknown",
    "remote_current",
    "remote_mismatch",
    "remote_lock_owned",
    "owner_live",
    "owner_stopping",
    "owner_dead",
    "owner_foreign",
    "owner_unfenced",
    "token_missing",
    "backup_required",
    "backup_failed",
    "migration_required",
    "nfs_alternative",
    "protocol_below",
})
OWNER_STATE_LABELS = {
    "live": "Live",
    "stopping": "Stopping",
    "dead": "Dead",
    "foreign": "Foreign",
    "unfenced": "Unfenced",
    "unknown": "Unknown",
}
_BOUNDED_META = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_FRONTEND_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX = "0123456789abcdef"


@dataclass(frozen=True)
class RemoteUpdateVersions:
    desktop: str | None
    remote: str | None
    served_ui: str | None
    protocol: str | None
    schema: str | None


@dataclass(frozen=True)
class RemoteUpdateOwner:
    state: str
    token_available: bool | None
    pidfd_available: bool | None
    process_exit: str
    listener_release: str
    lease_release: str


@dataclass(frozen=True)
class RemoteUpdateInstall:
    capability: str
    method: str


@dataclass(frozen=True)
class RemoteUpdateBackup:
    status: str
    migration_required: bool | None


@dataclass(frozen=True)
class RemoteUpdateSnapshot:
    schema_version: str
    stage: str
    code: str
    versions: RemoteUpdateVersions
    owner: RemoteUpdateOwner
    install: RemoteUpdateInstall
    backup: RemoteUpdateBackup
    actions: tuple[str, ...]


@dataclass(frozen=True)
class RemoteUpdatePresentation:
    headline: str
    detail: str
    next_action: str | None
    next_action_label: str
    notes: tuple[str, ...]
    pc_version: str
    pc_status: str
    server_version: str
    server_status: str
    served_ui_version: str
    served_ui_status: str
    owner_visible: bool
    owner_state: str
    token_availability: str
    diagnostics: tuple[tuple[str, str], ...]
    desktop_independent: bool


def unknown_snapshot() -> RemoteUpdateSnapshot:
    return RemoteUpdateSnapshot(
        schema_version=SCHEMA_VERSION,
        stage="unknown",
        code="unknown",
        versions=RemoteUpdateVersions(None, None, None, None, None),
        owner=RemoteUpdateOwner("unknown", None, None, "unknown", "unknown", "unknown"),
        install=RemoteUpdateInstall("unknown", "unknown"),
        backup=RemoteUpdateBackup("unknown", None),
        actions=(),
    )


def normalize_remote_update_snapshot(raw: object) -> RemoteUpdateSnapshot:
    """Project a snapshot or return unknown. Extra keys are dropped, never shown."""

    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return unknown_snapshot()
    versions = raw.get("versions")
    owner = raw.get("owner")
    install = raw.get("install")
    backup = raw.get("backup")
    return RemoteUpdateSnapshot(
        schema_version=SCHEMA_VERSION,
        stage=_one_of(raw.get("stage"), STAGES, "unknown"),
        code=_one_of(raw.get("code"), CODES, "unknown"),
        versions=RemoteUpdateVersions(
            desktop=_product_version(_mapping(versions).get("desktop")),
            remote=_product_version(_mapping(versions).get("remote")),
            served_ui=_served_ui_value(_mapping(versions).get("served_ui")),
            protocol=_meta_value(_mapping(versions).get("protocol")),
            schema=_meta_value(_mapping(versions).get("schema")),
        ),
        owner=RemoteUpdateOwner(
            state=_one_of(_mapping(owner).get("state"), OWNER_STATES, "unknown"),
            token_available=_tri_bool(_mapping(owner).get("token_available")),
            pidfd_available=_tri_bool(_mapping(owner).get("pidfd_available")),
            process_exit=_one_of(
                _mapping(owner).get("process_exit"), EVIDENCE_STATES, "unknown"
            ),
            listener_release=_one_of(
                _mapping(owner).get("listener_release"), EVIDENCE_STATES, "unknown"
            ),
            lease_release=_one_of(
                _mapping(owner).get("lease_release"), EVIDENCE_STATES, "unknown"
            ),
        ),
        install=RemoteUpdateInstall(
            capability=_one_of(
                _mapping(install).get("capability"), INSTALL_CAPABILITIES, "unknown"
            ),
            method=_one_of(_mapping(install).get("method"), INSTALL_METHODS, "unknown"),
        ),
        backup=RemoteUpdateBackup(
            status=_one_of(_mapping(backup).get("status"), BACKUP_STATUSES, "unknown"),
            migration_required=_tri_bool(_mapping(backup).get("migration_required")),
        ),
        actions=_normalized_actions(raw.get("actions")),
    )


def present_remote_update(snapshot: object) -> RemoteUpdatePresentation:
    """Default-screen copy: status, versions, owner facts, one next action."""

    snap = snapshot if isinstance(snapshot, RemoteUpdateSnapshot) else normalize_remote_update_snapshot(snapshot)
    remote_expected = _remote_expected(snap)
    desktop_independent = remote_expected and snap.versions.remote is None
    next_action = next_admissible_action(snap)
    pc_version, pc_status = _pc_version_row(snap)
    server_version, server_status = _server_version_row(snap, remote_expected)
    served_version, served_status = _served_ui_row(snap, remote_expected)
    return RemoteUpdatePresentation(
        headline=_headline(snap, remote_expected, next_action),
        detail=_detail(snap, remote_expected, next_action),
        next_action=next_action,
        next_action_label="" if next_action is None else ACTION_LABELS[next_action],
        notes=_notes(snap, remote_expected, desktop_independent),
        pc_version=pc_version,
        pc_status=pc_status,
        server_version=server_version,
        server_status=server_status,
        served_ui_version=served_version,
        served_ui_status=served_status,
        owner_visible=remote_expected,
        owner_state=OWNER_STATE_LABELS[snap.owner.state],
        token_availability=_token_label(snap.owner.token_available),
        diagnostics=_diagnostics(snap),
        desktop_independent=desktop_independent,
    )


def build_remote_update_html(
    snapshot: object,
    *,
    capability: str,
    theme: str = "dark",
) -> str:
    """Render a mountable page. Dynamic values are escaped; extras are omitted."""

    if not _is_capability(capability):
        raise ValueError("Remote update capability is invalid")
    snap = snapshot if isinstance(snapshot, RemoteUpdateSnapshot) else normalize_remote_update_snapshot(snapshot)
    presentation = present_remote_update(snap)
    theme = normalize_theme(theme)
    binding = json.dumps(
        {"capability": capability},
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    primary = ""
    if presentation.next_action and presentation.next_action != "close":
        primary = (
            f'<button id="primary" class="primary" data-operation="'
            f'{html.escape(presentation.next_action, quote=True)}">'
            f'{html.escape(presentation.next_action_label, quote=False)}</button>'
        )
    notes = "".join(
        f'<p class="note">{html.escape(note, quote=False)}</p>' for note in presentation.notes
    )
    owner = ""
    if presentation.owner_visible:
        owner = (
            '<table class="facts"><caption>Owner</caption><tbody>'
            f'<tr><th scope="row">Owner</th><td>{html.escape(presentation.owner_state, quote=False)}</td></tr>'
            f'<tr><th scope="row">Session token</th><td>{html.escape(presentation.token_availability, quote=False)}</td></tr>'
            "</tbody></table>"
        )
    diagnostics = "".join(
        f"<tr><th scope='row'>{html.escape(label, quote=False)}</th>"
        f"<td>{html.escape(value, quote=False)}</td></tr>"
        for label, value in presentation.diagnostics
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="color-scheme" content="{theme}">
<style>
*{{box-sizing:border-box}}html,body{{height:100%;margin:0;background:{theme_color(theme, 'bg.app')};color:{theme_color(theme, 'text.primary')};font:15px system-ui,sans-serif}}
::selection{{background:{theme_color(theme, 'selection.bg')};color:{theme_color(theme, 'text.primary')}}}
body{{display:grid;place-items:center;padding:32px}}main{{width:min(680px,100%);padding:32px;border:1px solid {theme_color(theme, 'border.default')};border-radius:18px;background:{theme_color(theme, 'surface.raised')};box-shadow:0 24px 80px {theme_color(theme, 'backdrop')}}}
.mark{{width:44px;height:44px;display:grid;place-items:center}}.mark svg{{width:44px;height:44px;display:block}}
h1{{margin:22px 0 10px;font-size:25px;line-height:1.2;text-wrap:balance}}p{{margin:0;color:{theme_color(theme, 'text.muted')};line-height:1.6;overflow-wrap:anywhere}}
.note{{margin-top:14px;padding:14px;border-radius:12px;background:{theme_color(theme, 'status.info.surface')};color:{theme_color(theme, 'status.info.text')};border:1px solid {theme_color(theme, 'status.info.border')}}}
table.facts{{width:100%;border-collapse:collapse;margin-top:22px}}table.facts caption{{text-align:left;font-weight:700;color:{theme_color(theme, 'text.primary')};padding-bottom:8px}}
table.facts th,table.facts td{{padding:8px 0;border-top:1px solid {theme_color(theme, 'border.subtle')};text-align:left;vertical-align:baseline}}
table.facts th{{width:42%;color:{theme_color(theme, 'text.muted')};font-weight:600}}table.facts td{{color:{theme_color(theme, 'text.primary')}}}
.actions{{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:10px;margin-top:28px}}button{{min-height:44px;padding:0 18px;border:1px solid {theme_color(theme, 'control.border')};border-radius:11px;background:{theme_color(theme, 'control.bg')};color:{theme_color(theme, 'text.primary')};font:inherit;font-weight:700;cursor:pointer}}
button.primary{{border-color:{theme_color(theme, 'selection.border')};background:{theme_color(theme, 'brand.accent')};color:{theme_color(theme, 'brand.ink')}}}button:hover{{background:{theme_color(theme, 'control.bgHover')}}}button.primary:hover{{background:{theme_color(theme, 'brand.accentStrong')}}}
button:focus-visible{{outline:3px solid {theme_color(theme, 'focus.ring')};outline-offset:3px}}button:disabled{{opacity:.55;cursor:wait}}
details{{margin-top:22px;color:{theme_color(theme, 'text.muted')}}}summary{{cursor:pointer;min-height:44px;display:flex;align-items:center;font-weight:700;color:{theme_color(theme, 'text.primary')}}}
details table.facts{{margin-top:8px}}
</style></head><body><main>{inline_mark_markup()}
<h1>{html.escape(presentation.headline, quote=False)}</h1>
<p>{html.escape(presentation.detail, quote=False)}</p>
<table class="facts"><caption>Versions</caption><tbody>
<tr><th scope="row">Update this PC</th><td>{html.escape(presentation.pc_version, quote=False)} — {html.escape(presentation.pc_status, quote=False)}</td></tr>
<tr><th scope="row">Update connected server</th><td>{html.escape(presentation.server_version, quote=False)} — {html.escape(presentation.server_status, quote=False)}</td></tr>
<tr><th scope="row">Served UI</th><td>{html.escape(presentation.served_ui_version, quote=False)} — {html.escape(presentation.served_ui_status, quote=False)}</td></tr>
</tbody></table>
{owner}{notes}
<div class="actions"><button id="close">Close</button>{primary}</div>
<details><summary>Diagnostics</summary>
<table class="facts"><tbody>{diagnostics}</tbody></table>
</details></main>
<script>
const binding={binding};
const uuid=()=>{{const b=crypto.getRandomValues(new Uint8Array(16));b[6]=b[6]&15|64;b[8]=b[8]&63|128;
 const h=Array.from(b,(v)=>v.toString(16).padStart(2,'0')).join('');
 return h.slice(0,8)+'-'+h.slice(8,12)+'-'+h.slice(12,16)+'-'+h.slice(16,20)+'-'+h.slice(20)}};
const post=(operation)=>window.chrome.webview.postMessage(JSON.stringify({{
 type:'{REMOTE_UPDATE_REQUEST_TYPE}',schema_version:1,request_id:uuid(),
 capability:binding.capability,operation
}}));
document.getElementById('close').addEventListener('click',()=>post('close'));
const action=document.querySelector('button.primary');
if(action)action.addEventListener('click',()=>{{action.disabled=true;post(action.dataset.operation)}});
</script></body></html>"""


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _one_of(value: object, allowed: frozenset[str], default: str) -> str:
    return value if value in allowed else default


def _tri_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


def _product_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    canonical = canonical_version(value)
    return canonical or None


def _served_ui_value(value: object) -> str | None:
    """Accept an observed served-UI identity; never copy desktop or remote."""

    if not isinstance(value, str):
        return None
    canonical = canonical_version(value)
    if canonical:
        return canonical
    if _FRONTEND_DIGEST.fullmatch(value):
        return value
    return None


def _meta_value(value: object) -> str | None:
    if not isinstance(value, str) or _BOUNDED_META.fullmatch(value) is None:
        return None
    return value


def _normalized_actions(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    actions: list[str] = []
    for item in value[:MAX_ACTIONS]:
        if item in ACTIONS and item not in actions:
            actions.append(item)
    return tuple(actions)


def _remote_expected(snapshot: RemoteUpdateSnapshot) -> bool:
    if snapshot.versions.remote is not None or snapshot.versions.served_ui is not None:
        return True
    if snapshot.owner.state != "unknown" or snapshot.owner.token_available is not None:
        return True
    if snapshot.install.capability != "unknown" or snapshot.install.method != "unknown":
        return True
    if snapshot.backup.status != "unknown" or snapshot.backup.migration_required is not None:
        return True
    if snapshot.stage not in {"idle", "unknown"}:
        return True
    if snapshot.code in REMOTE_CODES:
        return True
    return any(action in REMOTE_ACTIONS for action in snapshot.actions)


def _pc_version_row(snapshot: RemoteUpdateSnapshot) -> tuple[str, str]:
    version = snapshot.versions.desktop or "Unknown"
    if snapshot.code == "desktop_available" or "update_this_pc" in snapshot.actions:
        return version, "Update available"
    if snapshot.code == "desktop_blocked":
        return version, "Blocked"
    if snapshot.code == "desktop_current" or snapshot.code == "local_only":
        return version, "Current for this PC"
    if snapshot.versions.desktop:
        return version, "Observed"
    return version, "Unknown"


def _server_version_row(
    snapshot: RemoteUpdateSnapshot, remote_expected: bool
) -> tuple[str, str]:
    if not remote_expected:
        return "Not connected", "No connected server"
    version = snapshot.versions.remote or "Unknown"
    if snapshot.versions.remote is None:
        return version, "Unknown"
    desktop = snapshot.versions.desktop
    if desktop and snapshot.versions.remote == desktop:
        return version, "Same number as this PC, not a proven rebuild"
    if desktop:
        return version, "Different from this PC"
    return version, "Observed"


def _served_ui_row(
    snapshot: RemoteUpdateSnapshot, remote_expected: bool
) -> tuple[str, str]:
    if not remote_expected:
        return "Not connected", "No connected server"
    served = snapshot.versions.served_ui
    if served is None:
        return "Unknown", "Unknown"
    if served.startswith("sha256:"):
        return (
            "Observed digest",
            "Observed HTML entrypoint identity; not inferred from this PC or the remote product version",
        )
    version = served
    remote = snapshot.versions.remote
    if remote and served == remote:
        return version, "Same number as the remote product, not a proven rebuild"
    if remote:
        return version, "Different from the remote product"
    return version, "Observed"


def _token_label(value: bool | None) -> str:
    if value is True:
        return "Available"
    if value is False:
        return "Not available"
    return "Unknown"


def _evidence_label(value: str) -> str:
    return {"verified": "Verified", "failed": "Failed", "unknown": "Unknown"}[value]


def _terminal_headline(
    snapshot: RemoteUpdateSnapshot, remote_expected: bool, next_action: str | None
) -> str | None:
    """Outcomes that end or suspend the flow, read before owner or stage."""

    if snapshot.stage == "failed" or snapshot.code == "failed":
        return "The remote update did not finish."
    if snapshot.stage == "cancelled" or snapshot.code == "cancelled":
        return "The remote update was cancelled."
    if next_action == "reconcile_pending":
        return "The previous update step did not return an outcome."
    if snapshot.stage == "unknown" and remote_expected:
        return "The remote update state is unknown."
    if snapshot.stage == "ready" or snapshot.code == "ready":
        return "The connected server update is ready."
    return None


def _owner_headline(snapshot: RemoteUpdateSnapshot) -> str | None:
    """Owner authority, which outranks the stage the flow happens to be in."""

    if snapshot.owner.state == "stopping":
        return "The owner is stopping. That is not a completed shutdown."
    if snapshot.stage == "stop" and snapshot.owner.state == "live":
        return "The current owner must stop before backup."
    if snapshot.owner.state == "foreign" or snapshot.code == "owner_foreign":
        return "The connected server is owned by a foreign session."
    if snapshot.owner.state == "unfenced" or snapshot.code == "owner_unfenced":
        return "The connected server has an unfenced owner receipt."
    if snapshot.owner.state == "live" or snapshot.code in {
        "owner_live",
        "remote_lock_owned",
    }:
        return "The connected server is owned by a live session."
    return None


def _stage_headline(snapshot: RemoteUpdateSnapshot) -> str | None:
    """Where the remote update itself has reached."""

    if snapshot.stage == "backup" or snapshot.code == "backup_required":
        return "A verified backup is required before the new server starts."
    if snapshot.stage == "prepare":
        return "The connected server update is being prepared."
    if snapshot.stage == "probe":
        return "The replacement server is being probed."
    if snapshot.stage == "activate":
        return "Restart is required to finish the connected server activation."
    if snapshot.stage == "verify":
        return "The connected server update is being verified."
    if snapshot.stage == "preview":
        return "Review the connected server update before it starts."
    return None


def _version_headline(
    snapshot: RemoteUpdateSnapshot, remote_expected: bool, next_action: str | None
) -> str | None:
    """What the observed build numbers say once no flow condition applies."""

    if remote_expected and snapshot.versions.remote is None:
        return "This PC can update independently. The connected server version is unknown."
    if snapshot.code == "remote_mismatch" or (
        snapshot.versions.desktop
        and snapshot.versions.remote
        and snapshot.versions.desktop != snapshot.versions.remote
    ):
        return "This PC and the connected server are different builds."
    if next_action == "update_this_pc" or snapshot.code == "desktop_available":
        return "An update is available for this PC."
    if snapshot.code == "local_only" or not remote_expected:
        return "This PC is current."
    return None


def _headline(
    snapshot: RemoteUpdateSnapshot, remote_expected: bool, next_action: str | None
) -> str:
    """The status sentence, in the one fixed order these readings rank in."""

    for headline in (
        _terminal_headline(snapshot, remote_expected, next_action),
        _owner_headline(snapshot),
        _stage_headline(snapshot),
        _version_headline(snapshot, remote_expected, next_action),
    ):
        if headline is not None:
            return headline
    return "Update status"


def _detail(
    snapshot: RemoteUpdateSnapshot, remote_expected: bool, next_action: str | None
) -> str:
    parts: list[str] = []
    if snapshot.owner.state == "live" and snapshot.owner.token_available is False:
        parts.append(
            "A session token is not available, so this desktop cannot request "
            "a normal owner stop. That is not a legacy unfenced receipt."
        )
    elif snapshot.owner.state == "foreign":
        parts.append(
            "Work Stack will not force recovery on a foreign host. You can "
            "still update this PC."
        )
    elif snapshot.owner.state == "unfenced":
        parts.append(
            "The owner receipt is unfenced. A missing token and a foreign "
            "host are different facts, and this desktop will not force recovery."
        )
    elif snapshot.owner.state == "stopping":
        parts.append(
            "A stop was requested. Wait for verified process exit, listener "
            "release and lease release; a spawned stop command is not success."
        )
    action = {
        "update_this_pc": (
            "Update this PC. This does not update the connected server."
        ),
        "download_this_pc": (
            "Download the verified update for this PC. An offline connected "
            "server does not block this, and does not mean that server is current."
        ),
        "check_this_pc": (
            "Check for an update of this PC. Activation compatibility checks "
            "stay in force and are shown under diagnostics."
        ),
        "stop_owner": (
            "Stop this session's owner with the supported shutdown. Process "
            "exit, listener release and lease release are confirmed separately."
        ),
        "update_connected_server": (
            "Update the connected server. This does not replace this PC."
        ),
        "preview_server_update": (
            "Preview the connected server update. Nothing is activated yet."
        ),
        "restore_backup": (
            "Restore is explicit and uses the verified backup. It will not run "
            "old code against migrated data."
        ),
        "retry": "Retry the same operation identity. Do not start a different update blindly.",
        "retry_stop": (
            "Ask the same supported shutdown again. The owner answered that it "
            "is still running, so nothing is forced and the token is rechecked."
        ),
        "prepare_app_files": (
            "Stage the new application files beside the running one. This "
            "activates nothing, starts no server and touches no server data."
        ),
        "cancel": "Cancel leaves the previous app and profile in place.",
        "continue_flow": "Continue the current remote update step.",
        "restart": (
            "Restart continues the same pending activation receipt. "
            "It does not start a new mutation."
        ),
        "review": (
            "Open diagnostics for protocol, schema and shutdown evidence. They are not commands."
        ),
        "reconcile_pending": (
            "Check the same retained operation. This observes the outstanding "
            "request; it does not retry it or start a new mutation."
        ),
        "rollback_activation": (
            "Restore the previous application selection for this activation. "
            "This does not invent a pairing from an unknown state."
        ),
        "verify_connection": (
            "Verify the connected server that is now selected. This is a "
            "read-only check and does not require pretending the new owner is dead."
        ),
    }.get(next_action or "")
    if action:
        parts.append(action)
    elif remote_expected and snapshot.versions.remote is None:
        parts.append(
            "The connected server version is unknown, so it is not current. "
            "You can still discover or download an update for this PC."
        )
    elif not remote_expected:
        parts.append("This updater replaces the Windows desktop only.")
    elif not parts:
        parts.append("Choose the next action for the current condition.")
    return " ".join(parts)


def _owner_notes(snapshot: RemoteUpdateSnapshot) -> list[str]:
    """Owner state and session token stay separate facts."""

    notes: list[str] = []
    if snapshot.owner.state == "live" and snapshot.owner.token_available is False:
        notes.append(
            "Session token: not available. Owner state: live. These are separate facts."
        )
    if snapshot.owner.state == "unfenced" and snapshot.owner.token_available is False:
        notes.append(
            "Unfenced receipt and missing token are recorded separately. Neither authorizes force recovery."
        )
    return notes


def _install_notes(snapshot: RemoteUpdateSnapshot) -> list[str]:
    """What the measured install capability and method do and do not mean."""

    notes: list[str] = []
    if snapshot.install.method == "verified_unpack" or (
        snapshot.install.capability == "unavailable"
        and snapshot.install.method != "transactional"
    ):
        notes.append(
            "Transactional replace is not available here. Verified unpack into "
            "a new directory is the supported alternative. This does not mean "
            "every NFS atomic operation is unsupported."
        )
    elif snapshot.install.capability == "unknown" and snapshot.stage in {
        "preview",
        "prepare",
        "backup",
    }:
        notes.append("Install capability is unknown.")
    return notes


def _backup_notes(snapshot: RemoteUpdateSnapshot, remote_expected: bool) -> list[str]:
    """Backup evidence and the schema migration that evidence has to cover."""

    notes: list[str] = []
    if snapshot.backup.status == "not_run" and remote_expected:
        notes.append("A verified backup has not been run.")
    elif snapshot.backup.status == "verified":
        notes.append("Backup is verified.")
    elif snapshot.backup.status == "failed":
        notes.append("Backup failed. Do not start the new server against this data.")
    elif snapshot.backup.status == "unknown" and remote_expected and snapshot.stage in {
        "backup",
        "prepare",
        "probe",
        "activate",
    }:
        notes.append("Backup status is unknown.")
    if snapshot.backup.migration_required is True:
        notes.append("A schema migration is required.")
    elif snapshot.backup.migration_required is False and remote_expected:
        notes.append("No schema migration is required.")
    return notes


def _context_notes(
    snapshot: RemoteUpdateSnapshot, desktop_independent: bool
) -> list[str]:
    """Checks that constrain the remote server without constraining this PC."""

    notes: list[str] = []
    if snapshot.code == "protocol_below":
        notes.append(
            "Remote protocol is below the desktop requirement. That check does "
            "not block an unrelated update of this PC."
        )
    if desktop_independent:
        notes.append(
            "The connected server being unknown does not block checking or "
            "downloading an update for this PC."
        )
    return notes


def _notes(
    snapshot: RemoteUpdateSnapshot, remote_expected: bool, desktop_independent: bool
) -> tuple[str, ...]:
    """The collapsed note list, in the one fixed order the page reads them."""

    return (
        *_owner_notes(snapshot),
        *_install_notes(snapshot),
        *_backup_notes(snapshot, remote_expected),
        *_context_notes(snapshot, desktop_independent),
    )


def _diagnostics(snapshot: RemoteUpdateSnapshot) -> tuple[tuple[str, str], ...]:
    return (
        ("Stage", snapshot.stage),
        ("Code", snapshot.code),
        ("Protocol", snapshot.versions.protocol or "Unknown"),
        ("Schema", snapshot.versions.schema or "Unknown"),
        ("Served UI identity", snapshot.versions.served_ui or "Unknown"),
        ("pidfd", _token_label(snapshot.owner.pidfd_available)),
        ("Process exit", _evidence_label(snapshot.owner.process_exit)),
        ("Listener release", _evidence_label(snapshot.owner.listener_release)),
        ("Lease release", _evidence_label(snapshot.owner.lease_release)),
        ("Install capability", snapshot.install.capability),
        ("Install method", snapshot.install.method),
        ("Backup", snapshot.backup.status),
        (
            "Migration required",
            {True: "Yes", False: "No", None: "Unknown"}[snapshot.backup.migration_required],
        ),
    )


def _is_capability(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _CAPABILITY_LENGTH
        and all(character in _HEX for character in value)
    )
