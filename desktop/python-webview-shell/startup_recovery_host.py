"""Ephemeral native-host page, strict request parser, and recovery actions.

The page renders fixed copy plus one opaque CAS binding, and offers at most one
action.  Running that action stays here too, so the desktop host only has to
hand over a parsed request and show the page that comes back.
"""

from __future__ import annotations

import base64
import html
import json
import secrets
import threading
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Callable, Literal

from brand_assets import inline_mark_markup
from connection_registry_activation_recovery import (
    UNCERTAIN_RECONCILIATION_MESSAGE,
    ActivationReconciliationReport,
    ActivationRecoveryRefusedError,
    ConnectionRegistryActivationRecoveryService,
    activation_recovery_status_to_document,
)
from native_theme import normalize_theme, theme_color


#: The exact document source the native shell reports for this page, which is
#: loaded in memory with ``CoreWebView2.NavigateToString``.  A recovery request
#: is admitted only from one of these, never from an empty or foreign source.
#:
#: That source is *not* discriminating on its own: every in-memory document in
#: the same shell reports it.  It excludes http, https, file and data documents,
#: and the per-render capability below is what separates this page from any
#: other document that shares the same opaque source.
STARTUP_RECOVERY_DOCUMENT_SOURCES = frozenset({"about:blank"})

#: What ``CoreWebView2.NavigateToString`` reports as the *navigation* target for
#: the very document the shell just rendered.  Current runtimes inline the whole
#: document here as base64 behind this exact prefix; older ones report the
#: opaque in-memory source above instead.  A navigation target and a message
#: source are different facts: this prefix belongs to navigation admission only
#: and never widens the message source, which stays exactly ``about:blank``.
NAVIGATE_TO_STRING_PREFIX = "data:text/html;charset=utf-8;base64,"

#: Bytes of the unpredictable capability minted for each rendered recovery page.
RECOVERY_CAPABILITY_BYTES = 32
_CAPABILITY_LENGTH = RECOVERY_CAPABILITY_BYTES * 2

RECOVERY_REQUEST_TYPE = "workstack-connection-activation-recovery-request"
MAX_RECOVERY_REQUEST_BYTES = 2048
RESTORE_OPERATION = "restore-previous-connection"
RECONCILE_OPERATION = "reconcile-activation-evidence"

# Each explicit action and the advertised status flag that alone may offer it.
RECOVERY_ACTIONS = {
    RESTORE_OPERATION: "can_restore",
    RECONCILE_OPERATION: "can_reconcile",
}

# Fixed copy per action.  Nothing here is ever built from evidence or an error.
_ACTION_COPY = {
    RESTORE_OPERATION: {
        "button_id": "restore",
        "label": "Restore previous connection",
        "ready_title": "Work Stack could not open this workspace",
        "ready_detail": (
            "The newly selected workspace did not start. You can explicitly "
            "restore the previous connection without changing any SSOT content."
        ),
        "refused_title": "Connection could not be restored",
    },
    RECONCILE_OPERATION: {
        "button_id": "reconcile",
        "label": "Close duplicate records",
        "ready_title": "Work Stack found duplicate connection records",
        "ready_detail": (
            "An older version left more than one activation record. Work Stack can "
            "close the records that would restore nothing, leaving the single "
            "connection you can restore. Every record and rollback file is kept."
        ),
        "refused_title": "Duplicate records could not be closed",
    },
}
_REVIEW_TITLE = "Connection records need review"
_RESTART_DETAIL = "Close Work Stack, then open it again to use the restored connection."
_GENERIC_REFUSAL = (
    "Work Stack could not complete this recovery. "
    "Close Work Stack and inspect the connection again."
)


@dataclass(frozen=True)
class StartupRecoveryRequest:
    request_id: str
    activation_id: str
    capability: str
    operation: Literal[
        "restore-previous-connection", "reconcile-activation-evidence", "exit"
    ]
    expected_registry_digest: str | None = None


@dataclass(frozen=True)
class StartupRecoveryOutcome:
    """The status now advertised, its page, and whether a click may follow."""

    status: dict[str, object]
    page: str
    accepts_next_action: bool


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
        "capability",
        "operation",
    }
    expected_keys = (
        common if operation == "exit" else common | {"expected_registry_digest"}
    )
    if set(document) != expected_keys:
        return None
    if not _request_fields_are_valid(document, operation):
        return None
    digest = document.get("expected_registry_digest")
    if operation != "exit" and not _is_digest(digest):
        return None
    return StartupRecoveryRequest(
        request_id=document["request_id"],
        activation_id=document["activation_id"],
        capability=document["capability"],
        operation=operation,
        expected_registry_digest=digest,
    )


def _request_fields_are_valid(document: dict[str, object], operation: object) -> bool:
    """Check every exact field's shape before anything is bound to a page."""

    return (
        document.get("type") == RECOVERY_REQUEST_TYPE
        and document.get("schema_version") == 1
        and operation in RECOVERY_ACTIONS.keys() | {"exit"}
        and _is_canonical_uuid(document.get("request_id"))
        and _is_canonical_uuid(document.get("activation_id"))
        and _is_capability(document.get("capability"))
    )


def _require_renderable_page(
    status: dict[str, object],
    capability: str,
    outcome: str,
    safe_message: str,
) -> None:
    """Refuse to render anything that is not an exact, bounded page input."""

    if not _is_canonical_uuid(status.get("activation_id")) or not _is_digest(
        status.get("current_registry_digest")
    ):
        raise ValueError("Startup recovery status is not recoverable")
    if not _is_capability(capability):
        raise ValueError("Startup recovery capability is invalid")
    if outcome not in {"ready", "restored", "refused", "reviewed"}:
        raise ValueError("Startup recovery outcome is invalid")
    if safe_message and (len(safe_message) > 240 or any(ord(c) < 32 for c in safe_message)):
        raise ValueError("Startup recovery message is invalid")


def build_startup_recovery_html(
    status: dict[str, object],
    *,
    capability: str,
    outcome: Literal["ready", "restored", "refused", "reviewed"] = "ready",
    safe_message: str = "",
    theme: str = "dark",
) -> str:
    """Render fixed copy, opaque CAS bindings and this render's capability.

    The capability is minted once per rendered page and is the only thing that
    separates this document from any other in-memory document the shell reports
    under the same opaque source, so it is required and never defaulted.
    """

    _require_renderable_page(status, capability, outcome, safe_message)
    activation_id = status.get("activation_id")
    digest = status.get("current_registry_digest")
    offered = [
        operation
        for operation, flag in RECOVERY_ACTIONS.items()
        if status.get(flag) is True
    ]
    if len(offered) != 1:
        raise ValueError("Startup recovery status does not offer one action")
    action = offered[0]
    copy = _ACTION_COPY[action]

    title = {
        "ready": copy["ready_title"],
        "restored": "Previous connection restored",
        "refused": copy["refused_title"],
        "reviewed": _REVIEW_TITLE,
    }[outcome]
    detail = {
        "ready": copy["ready_detail"],
        "restored": _RESTART_DETAIL,
        "refused": safe_message or _GENERIC_REFUSAL,
        "reviewed": safe_message or _GENERIC_REFUSAL,
    }[outcome]
    binding = json.dumps(
        {
            "activation_id": activation_id,
            "expected_registry_digest": digest,
            "capability": capability,
        },
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    action_button = (
        f'<button id="{copy["button_id"]}" class="primary" data-operation="{action}">'
        f'{copy["label"]}</button>'
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
h1{{margin:22px 0 10px;font-size:25px;line-height:1.2;text-wrap:balance}}p{{margin:0;color:{theme_color(theme, 'text.muted')};line-height:1.6;overflow-wrap:anywhere}}.note{{margin-top:18px;padding:14px;border-radius:12px;background:{theme_color(theme, 'status.success.surface')};color:{theme_color(theme, 'status.success.text')}}}
.actions{{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:10px;margin-top:28px}}button{{min-height:44px;padding:0 18px;border:1px solid {theme_color(theme, 'control.border')};border-radius:11px;background:{theme_color(theme, 'control.bg')};color:{theme_color(theme, 'text.primary')};font:inherit;font-weight:700;cursor:pointer}}
button.primary{{border-color:{theme_color(theme, 'selection.border')};background:{theme_color(theme, 'brand.accent')};color:{theme_color(theme, 'brand.ink')}}}button:focus-visible{{outline:3px solid {theme_color(theme, 'focus.ring')};outline-offset:3px}}button:disabled{{opacity:.55;cursor:wait}}
</style></head><body><main>{inline_mark_markup()}
<h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>
<div class="note">Recovery changes only Work Stack's own connection records. It never edits the previous or failed SSOT.</div>
<div class="actions"><button id="exit">Exit</button>{action_button}</div></main>
<script>
const binding={binding};
// This page is loaded in memory, so it is not a secure context and the
// secure-context-gated uuid generator is absent.  getRandomValues is not
// gated, so mint the canonical version 4 request id the parser requires.
const uuid=()=>{{const b=crypto.getRandomValues(new Uint8Array(16));b[6]=b[6]&15|64;b[8]=b[8]&63|128;
 const h=Array.from(b,(v)=>v.toString(16).padStart(2,'0')).join('');
 return h.slice(0,8)+'-'+h.slice(8,12)+'-'+h.slice(12,16)+'-'+h.slice(16,20)+'-'+h.slice(20)}};
const post=(operation)=>window.chrome.webview.postMessage(JSON.stringify({{
 type:'{RECOVERY_REQUEST_TYPE}',schema_version:1,request_id:uuid(),
 activation_id:binding.activation_id,capability:binding.capability,operation,
 ...(operation==='exit'?{{}}:{{expected_registry_digest:binding.expected_registry_digest}})
}}));
document.getElementById('exit').addEventListener('click',()=>post('exit'));
const action=document.querySelector('button.primary');
if(action)action.addEventListener('click',()=>{{action.disabled=true;post(action.dataset.operation)}});
</script></body></html>"""


def startup_recovery_navigation_targets(page: str) -> frozenset[str]:
    """Every navigation target the shell's own render of exactly *page* reports.

    The data form carries the whole rendered document, so admitting it admits
    that document and nothing else: an arbitrary ``data:`` URL, or one that only
    shares the prefix, encodes different bytes and can never match.  The opaque
    form carries nothing, which is why the caller spends it as a one-shot token
    tied to a render the host itself just started.
    """

    inlined = base64.b64encode(page.encode("utf-8")).decode("ascii")
    return frozenset(
        {NAVIGATE_TO_STRING_PREFIX + inlined} | STARTUP_RECOVERY_DOCUMENT_SOURCES
    )


def _navigation_candidates(target: str) -> set[str]:
    """The target as reported, plus its decoded form if it was escaped."""

    if "%" not in target:
        return {target}
    return {target, urllib.parse.unquote(target)}


def mint_startup_recovery_capability() -> str:
    """Mint one unpredictable capability for exactly one rendered page."""

    return secrets.token_hex(RECOVERY_CAPABILITY_BYTES)


def startup_recovery_capability_matches(active: object, offered: object) -> bool:
    """Admit only the capability minted for the page that is now on screen."""

    return (
        _is_capability(active)
        and _is_capability(offered)
        and secrets.compare_digest(active, offered)
    )


def startup_recovery_request_is_bound(
    request: StartupRecoveryRequest,
    status: dict[str, object],
    capability: object,
) -> bool:
    """Report whether one parsed request belongs to the page now on screen.

    Every operation is bound, exit included: a document that never held this
    render's capability cannot close the window any more than it can act.  An
    action additionally has to name the registry state that was advertised.
    """

    if request.activation_id != status.get("activation_id"):
        return False
    if not startup_recovery_capability_matches(capability, request.capability):
        return False
    return request.operation == "exit" or (
        request.expected_registry_digest == status.get("current_registry_digest")
    )


def startup_recovery_action_is_offered(
    status: dict[str, object], operation: str
) -> bool:
    """Report whether the rendered status actually advertised this operation."""

    flag = RECOVERY_ACTIONS.get(operation)
    return flag is not None and status.get(flag) is True


def apply_startup_recovery_action(
    service: ConnectionRegistryActivationRecoveryService,
    status: dict[str, object],
    *,
    operation: str,
    activation_id: str,
    expected_registry_digest: str,
    capability: str,
    theme: str,
) -> StartupRecoveryOutcome:
    """Run one explicit recovery action and render the page it leaves behind.

    Reconciliation reports what it actually wrote.  Only a fully committed one
    that still leaves the advertised recovery re-renders a fresh single-receipt
    restore page, so the following click is bound to evidence read after the
    write; every other completed reconciliation renders a truthful review page
    with no action.  A refusal precedes the first write, so it alone may keep
    the advertised status and its fixed refusal copy; an unexpected failure of a
    reconciliation carries no such proof and is rendered as review, not as a
    refusal.  No failure path leaves an action pending.
    """

    try:
        if operation == RECONCILE_OPERATION:
            return _reconciled_outcome(
                service.reconcile(
                    activation_id,
                    expected_registry_digest=expected_registry_digest,
                ),
                status,
                capability,
                theme,
            )
        if operation != RESTORE_OPERATION:
            raise ValueError("Startup recovery operation is not supported")
        service.restore(
            activation_id, expected_registry_digest=expected_registry_digest
        )
        page = build_startup_recovery_html(
            status, capability=capability, outcome="restored", theme=theme
        )
    except ActivationRecoveryRefusedError as error:
        page = build_startup_recovery_html(
            status,
            capability=capability,
            outcome="refused",
            safe_message=error.safe_message,
            theme=theme,
        )
    except Exception:
        page = _unexpected_failure_page(status, capability, operation, theme)
    return StartupRecoveryOutcome(status, page, False)


def _unexpected_failure_page(
    status: dict[str, object], capability: str, operation: str, theme: str
) -> str:
    """Render an unexpected failure without claiming more than is known.

    The refusal copy is the class that promises nothing was written, and the
    recovery service raises one only from a path that proved it.  An unexpected
    failure of a reconciliation is not that proof: records may already have
    moved, so it shows the same non-actionable review page an unconfirmed
    reconciliation shows.  Every other operation keeps its existing refusal.
    """

    if operation != RECONCILE_OPERATION:
        return build_startup_recovery_html(
            status, capability=capability, outcome="refused", theme=theme
        )
    return build_startup_recovery_html(
        status,
        capability=capability,
        outcome="reviewed",
        safe_message=UNCERTAIN_RECONCILIATION_MESSAGE,
        theme=theme,
    )


def _reconciled_outcome(
    report: ActivationReconciliationReport,
    status: dict[str, object],
    capability: str,
    theme: str,
) -> StartupRecoveryOutcome:
    """Offer the resumable restore only when the records really support it.

    Any other completed reconciliation changed evidence the advertised page no
    longer describes, so it shows the fixed review copy for the state that was
    actually proven.  The advertised binding is retained only so the page's Exit
    still resolves; it can never be acted on again because no action is offered
    and the caller keeps the flow closed.
    """

    if report.state == "resumed" and report.status is not None:
        refreshed = activation_recovery_status_to_document(report.status)
        return StartupRecoveryOutcome(
            refreshed,
            build_startup_recovery_html(refreshed, capability=capability, theme=theme),
            True,
        )
    page = build_startup_recovery_html(
        status,
        capability=capability,
        outcome="reviewed",
        safe_message=report.message,
        theme=theme,
    )
    return StartupRecoveryOutcome(status, page, False)


@dataclass(frozen=True)
class StartupRecoveryAdmission:
    """What one parsed message is allowed to do to the page now on screen."""

    outcome: Literal["unbound", "exit", "ignored", "action"]
    request: StartupRecoveryRequest | None = None
    status: dict[str, object] | None = None
    generation: int = -1


class StartupRecoveryLifetime:
    """One synchronized lifetime for the recovery page that is on screen.

    Every published page owns a generation and the capability minted for it.
    An action captures its generation before it starts and may publish only
    while that generation is still the live one, so a worker whose page was
    exited, navigated away from or replaced can neither rearm recovery nor
    render over whatever took its place.

    Nothing here cancels work that is already running: a service call that has
    committed stays committed, and only the page that would have described it
    is dropped.  Invalidation happens inside the lock and disposal outside it,
    so a caller never closes a window that is still advertising a capability.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.status: dict[str, object] | None = None
        self.capability: str | None = None
        self.in_progress = False
        self.closed = False
        self.generation = 0
        self._expected_documents: list[frozenset[str]] = []

    def active_generation(self) -> int | None:
        """Capture the generation an action starting now may publish into."""

        with self._lock:
            return None if self.closed else self.generation

    def status_for(self, generation: int) -> dict[str, object] | None:
        """The advertised status, but only while its generation is still live."""

        with self._lock:
            if self.closed or self.generation != generation:
                return None
            return self.status

    def admit_request(self, message: str) -> StartupRecoveryAdmission:
        """Classify one message against the page that is really on screen.

        Exit invalidates here, inside the lock, so the caller disposes the
        window with the capability and status already retired and a replay from
        the document that is going away can do nothing at all.
        """

        with self._lock:
            status = self.status
            if status is None or self.closed:
                return StartupRecoveryAdmission("unbound")
            request = parse_startup_recovery_request(message)
            if request is None or not startup_recovery_request_is_bound(
                request, status, self.capability
            ):
                return StartupRecoveryAdmission("unbound")
            if request.operation == "exit":
                self.retire(closed=True)
                return StartupRecoveryAdmission("exit", request)
            if self.in_progress or not startup_recovery_action_is_offered(
                status, request.operation
            ):
                return StartupRecoveryAdmission("ignored", request)
            self.in_progress = True
            return StartupRecoveryAdmission("action", request, status, self.generation)

    def publish(
        self,
        generation: int,
        status: dict[str, object],
        capability: str,
        page: str,
        render: Callable[[str], None],
        *,
        accepts_next_action: bool,
    ) -> bool:
        """Adopt one freshly rendered page, then put exactly that page up.

        Adoption rotates the capability, retires the generation the caller
        started from and arms exactly one expected render — the navigation
        *this* document will report — so a superseded worker publishes nothing,
        renders nothing, and leaves no admission behind it.  A render that never
        reaches the screen retires the page it would otherwise have armed.
        """

        with self._lock:
            if self.closed or self.generation != generation:
                return False
            self.generation = published = generation + 1
            self.status = status
            self.capability = capability
            self.in_progress = not accepts_next_action
            self._expected_documents = [startup_recovery_navigation_targets(page)]
        try:
            render(page)
        except Exception:
            self.retire(generation=published)
            return False
        return True

    def retire(self, *, generation: int | None = None, closed: bool = False) -> bool:
        """Invalidate the page on screen so nothing stale can act or render."""

        with self._lock:
            if generation is not None and self.generation != generation:
                return False
            self.status = None
            self.capability = None
            self.in_progress = False
            self.generation += 1
            # The armed target embeds the rendered document, capability and
            # all, so a retired page is dropped here rather than held on.
            self._expected_documents.clear()
            self.closed = self.closed or closed
            return True

    def admits_navigation(self, target: str) -> bool:
        """Admit only a document one of our own renders started, exactly once.

        A render arms the targets that exact document reports and nothing else,
        so an arbitrary ``data:`` URL, a foreign one sharing the same prefix, or
        an ``about:blank`` no render of ours started is not this page and is not
        admitted.  The match consumes the render that armed it, so replaying an
        admitted target, or navigating again after the page was retired, is
        refused like any other foreign document.
        """

        with self._lock:
            if self.closed or self.status is None:
                return False
            candidates = _navigation_candidates(target)
            for index, armed in enumerate(self._expected_documents):
                if candidates & armed:
                    del self._expected_documents[index]
                    return True
            return False


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.int != 0 and str(parsed) == value


def _is_capability(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _CAPABILITY_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )
