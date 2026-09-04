"""Ephemeral native-host page and strict request parser for startup recovery."""

from __future__ import annotations

import html
import json
import uuid
from dataclasses import dataclass
from typing import Literal

from brand_assets import inline_mark_markup
from native_theme import normalize_theme, theme_color


RECOVERY_REQUEST_TYPE = "workstack-connection-activation-recovery-request"
MAX_RECOVERY_REQUEST_BYTES = 2048


@dataclass(frozen=True)
class StartupRecoveryRequest:
    request_id: str
    activation_id: str
    operation: Literal["restore-previous-connection", "exit"]
    expected_registry_digest: str | None = None


def parse_startup_recovery_request(message: str) -> StartupRecoveryRequest | None:
    if not isinstance(message, str):
        return None
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_RECOVERY_REQUEST_BYTES:
        return None
    try:
        document = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    operation = document.get("operation")
    common = {
        "type",
        "schema_version",
        "request_id",
        "activation_id",
        "operation",
    }
    expected_keys = (
        common | {"expected_registry_digest"}
        if operation == "restore-previous-connection"
        else common
    )
    if set(document) != expected_keys:
        return None
    if (
        document.get("type") != RECOVERY_REQUEST_TYPE
        or document.get("schema_version") != 1
        or operation not in {"restore-previous-connection", "exit"}
        or not _is_canonical_uuid(document.get("request_id"))
        or not _is_canonical_uuid(document.get("activation_id"))
    ):
        return None
    digest = document.get("expected_registry_digest")
    if operation == "restore-previous-connection" and not _is_digest(digest):
        return None
    return StartupRecoveryRequest(
        request_id=document["request_id"],
        activation_id=document["activation_id"],
        operation=operation,
        expected_registry_digest=digest,
    )


def build_startup_recovery_html(
    status: dict[str, object],
    *,
    outcome: Literal["ready", "restored", "refused"] = "ready",
    safe_message: str = "",
    theme: str = "dark",
) -> str:
    """Render only fixed copy and opaque CAS bindings into an in-memory page."""

    activation_id = status.get("activation_id")
    digest = status.get("current_registry_digest")
    if not _is_canonical_uuid(activation_id) or not _is_digest(digest):
        raise ValueError("Startup recovery status is not recoverable")
    if outcome not in {"ready", "restored", "refused"}:
        raise ValueError("Startup recovery outcome is invalid")
    if safe_message and (len(safe_message) > 240 or any(ord(c) < 32 for c in safe_message)):
        raise ValueError("Startup recovery message is invalid")

    title = {
        "ready": "Work Stack could not open this workspace",
        "restored": "Previous connection restored",
        "refused": "Connection could not be restored",
    }[outcome]
    detail = {
        "ready": (
            "The newly selected workspace did not start. You can explicitly restore "
            "the previous connection without changing any SSOT content."
        ),
        "restored": "Close Work Stack, then open it again to use the restored connection.",
        "refused": safe_message or "The recovery evidence changed. Close Work Stack and inspect the connection again.",
    }[outcome]
    binding = json.dumps(
        {"activation_id": activation_id, "expected_registry_digest": digest},
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    restore_button = (
        '<button id="restore" class="primary">Restore previous connection</button>'
        if outcome == "ready"
        else ""
    )
    theme = normalize_theme(theme)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="color-scheme" content="{theme}">
<style>
*{{box-sizing:border-box}}html,body{{height:100%;margin:0;background:{theme_color(theme, 'bg.app')};color:{theme_color(theme, 'text.primary')};font:15px system-ui,sans-serif}}
body{{display:grid;place-items:center;padding:32px}}main{{width:min(620px,100%);padding:32px;border:1px solid {theme_color(theme, 'border.default')};border-radius:18px;background:{theme_color(theme, 'surface.raised')};box-shadow:0 24px 80px {theme_color(theme, 'backdrop')}}}
.mark{{width:44px;height:44px;display:grid;place-items:center}}.mark svg{{width:44px;height:44px;display:block}}
h1{{margin:22px 0 10px;font-size:25px;line-height:1.2}}p{{margin:0;color:{theme_color(theme, 'text.muted')};line-height:1.6}}.note{{margin-top:18px;padding:14px;border-radius:12px;background:{theme_color(theme, 'status.success.surface')};color:{theme_color(theme, 'status.success.text')}}}
.actions{{display:flex;justify-content:flex-end;gap:10px;margin-top:28px}}button{{min-height:44px;padding:0 18px;border:1px solid {theme_color(theme, 'control.border')};border-radius:11px;background:{theme_color(theme, 'control.bg')};color:{theme_color(theme, 'text.primary')};font:inherit;font-weight:700;cursor:pointer}}
button.primary{{border-color:{theme_color(theme, 'selection.border')};background:{theme_color(theme, 'brand.accent')};color:{theme_color(theme, 'brand.ink')}}}button:focus-visible{{outline:3px solid {theme_color(theme, 'focus.ring')};outline-offset:3px}}button:disabled{{opacity:.55;cursor:wait}}
</style></head><body><main>{inline_mark_markup()}
<h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>
<div class="note">Recovery changes only the connection registry. Work Stack will not edit the previous or failed SSOT.</div>
<div class="actions"><button id="exit">Exit</button>{restore_button}</div></main>
<script>
const binding={binding};
const post=(operation)=>window.chrome.webview.postMessage(JSON.stringify({{
 type:'{RECOVERY_REQUEST_TYPE}',schema_version:1,request_id:crypto.randomUUID(),
 activation_id:binding.activation_id,operation,
 ...(operation==='restore-previous-connection'?{{expected_registry_digest:binding.expected_registry_digest}}:{{}})
}}));
document.getElementById('exit').addEventListener('click',()=>post('exit'));
const restore=document.getElementById('restore');
if(restore)restore.addEventListener('click',()=>{{restore.disabled=true;post('restore-previous-connection')}});
</script></body></html>"""


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.int != 0 and str(parsed) == value


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )
