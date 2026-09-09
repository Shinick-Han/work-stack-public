"""The words the update screen says about the optional agent Skill.

The Skill step is a side action on a finished remote update, so every sentence
here has one job the rest of the screen's copy does not: to say what happened
to the Skill in the operator's home directory *without* implying that the
remote update, which by then has already succeeded, did anything but succeed.
Each headline therefore keeps the ready sentence and adds its own.

This module holds no logic beyond three lookups and performs no effect.  It is
separate from ``remote_update_presentation`` so the copy for an optional step
can grow without pushing the page renderer past its size budget.
"""

from __future__ import annotations


#: The optional agent-Skill side action's own codes, exactly the E projection
#: of ``remote_update_skill_port.SKILL_FLOW_CODES``.  Every one is published at
#: stage ``ready``.
SKILL_CODES = frozenset({
    "skill_absent",
    "skill_outdated",
    "skill_current",
    "skill_installed",
    "skill_updated",
    "skill_refused_app_not_verified",
    "skill_refused_destination_foreign",
    "skill_refused_destination_modified",
    "skill_refused_failed",
    "skill_unknown",
})

READY_SENTENCE = "The connected server update is ready."

SKILL_HEADLINES = {
    "skill_absent": "The agent Skill is not installed for this account.",
    "skill_outdated": "The agent Skill installed for this account is an older build.",
    "skill_current": "The agent Skill is already current for this account.",
    "skill_installed": "The agent Skill was installed for this account.",
    "skill_updated": "The agent Skill was updated for this account.",
    "skill_refused_app_not_verified": (
        "The agent Skill was not installed: the verified application was not confirmed."
    ),
    "skill_refused_destination_foreign": (
        "The agent Skill was not installed: the destination holds files Work Stack does not own."
    ),
    "skill_refused_destination_modified": (
        "The agent Skill was not installed: the installed files were edited after the last install."
    ),
    "skill_refused_failed": "The agent Skill was not installed.",
    "skill_unknown": "The agent Skill result is not known.",
}

#: What the operator can actually do about each answer.  A refusal says what
#: was kept and what to do; a lost answer says plainly that it is not known,
#: and that checking again is the next step rather than writing again.
SKILL_DETAILS = {
    "skill_current": "Nothing was written. The installed files already match this build.",
    "skill_refused_app_not_verified": (
        "Nothing was written and no home directory was touched."
    ),
    "skill_refused_destination_foreign": (
        "Nothing was written. Move or remove what is at that path yourself, then "
        "check again. Work Stack will not overwrite files it did not install."
    ),
    "skill_refused_destination_modified": (
        "Nothing was written and your edits were kept. Restore or remove the "
        "edited files yourself, then check again."
    ),
    "skill_refused_failed": (
        "The helper refused. Check again to read what is actually installed."
    ),
    "skill_unknown": (
        "The answer did not arrive, so it is not known whether anything was "
        "written. Check again: that reads the files that are actually there. "
        "Nothing is written a second time on its own."
    ),
}

#: What each offered button will do, said before it is clicked.
SKILL_ACTION_DETAILS = {
    "inspect_skill": (
        "Check whether the Work Stack agent Skill is installed for your account "
        "on the connected server. This reads only; it writes nothing."
    ),
    "install_skill": (
        "Install the Work Stack agent Skill into your home directory on the "
        "connected server. It writes only that Skill directory and changes no "
        "shell profile, no server data and no connection selection."
    ),
    "update_skill": (
        "Replace the older Work Stack agent Skill in your home directory on the "
        "connected server with this build's. It writes only that Skill directory "
        "and changes no shell profile and no server data."
    ),
}


def skill_headline(code: object) -> str | None:
    """The status sentence for one Skill code, or nothing for any other code."""

    sentence = SKILL_HEADLINES.get(code) if isinstance(code, str) else None
    return None if sentence is None else f"{READY_SENTENCE} {sentence}"


def skill_detail(code: object) -> str:
    """What the operator can do about one Skill answer, or nothing to add."""

    return SKILL_DETAILS.get(code, "") if isinstance(code, str) else ""


def skill_action_detail(action: object) -> str:
    """What one offered Skill button will do, or nothing for any other action."""

    return SKILL_ACTION_DETAILS.get(action, "") if isinstance(action, str) else ""


__all__ = [
    "READY_SENTENCE",
    "SKILL_ACTION_DETAILS",
    "SKILL_CODES",
    "SKILL_DETAILS",
    "SKILL_HEADLINES",
    "skill_action_detail",
    "skill_detail",
    "skill_headline",
]
