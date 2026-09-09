"""Admissible user actions for the remote update page.

Action allowlists and the one-next-action gate live here so the HTML
renderer stays inside the production line budget while Integration-4
adds reconcile, rollback, and read-only verify vocabulary.
"""

from __future__ import annotations

from typing import Any


MAX_ACTIONS = 8
RELEASE_FIELDS = ("process_exit", "listener_release", "lease_release")

ACTIONS = frozenset({
    "update_this_pc",
    "download_this_pc",
    "check_this_pc",
    "update_connected_server",
    "preview_server_update",
    "stop_owner",
    "retry",
    "retry_stop",
    "cancel",
    "restore_backup",
    "prepare_app_files",
    "continue_flow",
    "restart",
    "close",
    "review",
    "reconcile_pending",
    "rollback_activation",
    "verify_connection",
    "inspect_skill",
    "install_skill",
    "update_skill",
})
REMOTE_ACTIONS = frozenset({
    "update_connected_server",
    "preview_server_update",
    "stop_owner",
    "restore_backup",
    "prepare_app_files",
    "continue_flow",
    "restart",
    "retry",
    "retry_stop",
    "reconcile_pending",
    "rollback_activation",
    "verify_connection",
    "inspect_skill",
    "install_skill",
    "update_skill",
})
THIS_PC_ACTIONS = frozenset({
    "update_this_pc",
    "download_this_pc",
    "check_this_pc",
})
READ_ONLY_ACTIONS = frozenset({
    "preview_server_update",
    "cancel",
    "close",
    "review",
    "reconcile_pending",
    "verify_connection",
    "inspect_skill",
})
#: The agent-Skill offer, which is a side action on an already finished update.
#: ``inspect_skill`` writes nothing anywhere; the two write actions are the
#: explicit second click and are deliberately *not* new remote mutations,
#: because neither starts, retries or resumes a remote update attempt.
SKILL_ACTIONS = frozenset({"inspect_skill", "install_skill", "update_skill"})
#: The exact published code each write offer requires.  An install may only be
#: clicked from the inspect that found nothing installed, and an update only
#: from the inspect that found an older build; anything else, an unknown answer
#: included, admits neither.
SKILL_OFFER_CODES = {
    "install_skill": "skill_absent",
    "update_skill": "skill_outdated",
}
NEW_REMOTE_MUTATIONS = frozenset({
    "update_connected_server",
    "retry",
    "restore_backup",
    "continue_flow",
})
ACTION_LABELS = {
    "update_this_pc": "Update this PC",
    "download_this_pc": "Download this PC update",
    "check_this_pc": "Check for this PC update",
    "update_connected_server": "Update connected server",
    "preview_server_update": "Preview server update",
    "stop_owner": "Stop this session's owner",
    "retry": "Retry",
    "cancel": "Cancel",
    "restore_backup": "Restore the verified backup",
    "prepare_app_files": "Stage the new app files",
    "retry_stop": "Try stopping the owner again",
    "continue_flow": "Continue",
    "restart": "Restart to finish activation",
    "close": "Close",
    "review": "Review diagnostics",
    "reconcile_pending": "Check pending outcome",
    "rollback_activation": "Restore previous app selection",
    "verify_connection": "Verify the connected server",
    "inspect_skill": "Check agent Skill",
    "install_skill": "Install agent Skill",
    "update_skill": "Update agent Skill",
}


def owner_allows_remote_mutation(snapshot: object) -> bool:
    owner = getattr(snapshot, "owner", None)
    return _owner_state(owner) == "dead" and quiescence_verified(snapshot)


def quiescence_verified(snapshot: object) -> bool:
    owner = getattr(snapshot, "owner", None)
    return all(
        _owner_field(owner, name) == "verified" for name in RELEASE_FIELDS
    )


def owner_stop_evidence_measured(snapshot: object) -> bool:
    """True once an owner stop call has actually answered with evidence.

    Every field of the owner record starts at its unmeasured reading and the
    flow replaces the whole record with what the stop port returned, so any
    departure from that reading is an observation rather than a guess.
    """

    owner = getattr(snapshot, "owner", None)
    if _owner_state(owner) != "unknown":
        return True
    if _owner_field(owner, "token_available") is not None:
        return True
    if _owner_field(owner, "pidfd_available") is not None:
        return True
    return any(_owner_field(owner, name) != "unknown" for name in RELEASE_FIELDS)


def stop_offer_admits(snapshot: object) -> bool:
    """Admit the stop only from the offer the flow is making right now.

    The flow offers the stop before any owner evidence exists, because calling
    the authenticated owner stop port *is* the authority check: the token, the
    owner state and the three release observations are what that call returns.
    Demanding them beforehand would require of the click exactly the facts the
    click obtains, so the first mutating step could never run.  Admitting the
    offer pretends nothing -- the page still reports the owner as unknown, and
    only the port decides.  Once that call has answered, its measured evidence
    rules: a missing token, a foreign host or an unfenced receipt stays
    refused, and an offer that is arbitrary, stale or absent is never admitted.
    """

    if "stop_owner" not in getattr(snapshot, "actions", ()):
        return False
    if not owner_stop_evidence_measured(snapshot):
        return True
    owner = getattr(snapshot, "owner", None)
    return (
        _owner_state(owner) == "live"
        and _owner_field(owner, "token_available") is True
    )


def prepare_offer_admits(snapshot: object) -> bool:
    """Admit code-only staging of the new application from its offer alone.

    This action carries exactly one D operation, ``run_prepare_code_only``,
    which the flow offers only in the explicitly selected prepare-before-stop
    order.  That call writes an absent new application directory and verifies
    the archive, the sidecar, every payload hash, the imports and the receipt;
    it reads and writes no SSOT, starts no server and changes no registry or
    activation selection.  The quiescence gate exists to protect the live data,
    so it does not describe this call -- and since this staging runs precisely
    while the owner is still serving (that is the whole point of the order,
    because the helpers the later stages need live inside the tree it stages),
    demanding a dead owner here would make the order unreachable rather than
    safer.

    The default order's full ``run_prepare`` is deliberately *not* this action.
    It stays on ``continue_flow`` behind the dead-plus-quiesced gate, and so do
    the backup, the probe, the activation and the restore in both orders.
    """

    return "prepare_app_files" in getattr(snapshot, "actions", ())


def stop_retry_admits(snapshot: object) -> bool:
    """Admit re-running the stop only where the owner is measurably live.

    This is the one condition an authenticated stop can be repeated in: the
    call answered, the session token was available, the host was this one, and
    what it found was an owner still running.  Asking that same supported
    shutdown again is then legal, and is the only way the journey continues.

    It is deliberately not a general retry.  A missing token, a foreign host,
    an unfenced receipt and an unsettled commit each publish their own code and
    none of them is admitted here; nothing is forced, and the answer still
    comes from the owner stop port rather than from this rule.
    """

    if "retry_stop" not in getattr(snapshot, "actions", ()):
        return False
    if getattr(snapshot, "code", None) != "owner_live":
        return False
    owner = getattr(snapshot, "owner", None)
    return (
        _owner_state(owner) == "live"
        and _owner_field(owner, "token_available") is True
    )


def rollback_pairing_admits(snapshot: object) -> bool:
    """Admit rollback only from an offered pairing, never from unknown."""

    if getattr(snapshot, "code", None) == "unknown":
        return False
    actions = getattr(snapshot, "actions", ())
    return "rollback_activation" in actions


def skill_offer_admits(snapshot: object, action: str) -> bool:
    """Admit an agent-Skill action only from the condition that offers it.

    The offer exists only on a finished update, so nothing here is reachable
    before stage ``ready``.  The read is admissible wherever it is offered; a
    write is admissible only from the published code of the inspect that
    earned it, which is what makes the inspect mandatory and makes a lost
    answer re-readable rather than re-writable.
    """

    if getattr(snapshot, "stage", None) != "ready":
        return False
    if action not in getattr(snapshot, "actions", ()):
        return False
    if action == "inspect_skill":
        return True
    return getattr(snapshot, "code", None) == SKILL_OFFER_CODES.get(action)


def action_is_admissible(snapshot: object, action: str) -> bool:
    if action not in ACTIONS:
        return False
    if action in SKILL_ACTIONS:
        return skill_offer_admits(snapshot, action)
    if action == "stop_owner":
        return stop_offer_admits(snapshot)
    if action == "prepare_app_files":
        return prepare_offer_admits(snapshot)
    if action == "retry_stop":
        return stop_retry_admits(snapshot)
    if action in THIS_PC_ACTIONS or action in READ_ONLY_ACTIONS:
        return True
    if action == "rollback_activation":
        return rollback_pairing_admits(snapshot)
    if action == "restart":
        return getattr(snapshot, "stage", None) == "activate"
    if action not in NEW_REMOTE_MUTATIONS:
        return False
    if not owner_allows_remote_mutation(snapshot):
        return False
    return _new_remote_mutation_admits(snapshot, action)


def next_admissible_action(snapshot: object) -> str | None:
    for action in getattr(snapshot, "actions", ()):
        if action != "close" and action_is_admissible(snapshot, action):
            return action
    return None


def _new_remote_mutation_admits(snapshot: object, action: str) -> bool:
    if action == "restore_backup":
        backup = getattr(snapshot, "backup", None)
        return getattr(backup, "status", None) == "verified"
    if action != "continue_flow":
        return action in {"update_connected_server", "retry"}
    stage = getattr(snapshot, "stage", None)
    if stage in {"unknown", "failed", "cancelled", "activate"}:
        return False
    if stage == "stop":
        return quiescence_verified(snapshot)
    return True


def _owner_state(owner: object) -> Any:
    return _owner_field(owner, "state")


def _owner_field(owner: object, name: str) -> Any:
    if owner is None:
        return None
    return getattr(owner, name, None)
