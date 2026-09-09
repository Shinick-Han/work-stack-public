"""Vocabulary and strict admission for the optional agent-Skill side action.

A finished remote update leaves a verified application directory on the far
host.  That tree already carries the packaged ``work-stack`` agent Skill and a
helper, ``remote_skill_install.py``, that can copy it into the operator's
``$HOME``.  This module is the vocabulary half of the offer the update screen
makes about that: the typed fact one helper run produces, the port protocol the
flow calls, and the one named mapping from the helper's raw document to the
flow's published code.

Nothing here performs an effect.  It reads bytes that a remote process wrote
and answers what they say, so every bound and every closed set below is what
keeps an unexpected or hostile document from being read as a success.

Three properties are this module's own responsibility.

*   The helper's document is admitted whole or not at all.  A process that
    exited zero is not evidence: the exact key set, the fixed operation, the
    schema, the destination, the three file digests and the exit code have to
    agree with each other, and with the artifact the update actually prepared.
*   A refusal is a refusal, never a silent nothing-happened.  An ``--apply``
    run whose answer never arrived is ``unknown``, which is a different fact
    from a helper that answered "I refused", and the two must not collapse.
*   The mapping from the helper's ``(action, outcome, code)`` triple to a
    published code is a named table here, not an invented status somewhere
    else.  The helper's own words are kept on the outcome beside it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


#: The helper's own document schema, frozen against ``remote_skill_install``.
HELPER_SCHEMA_VERSION = 1
HELPER_OPERATION = "install-skill"
HELPER_DESTINATION = ".agents/skills/work-stack"

#: The three destination-relative files the helper publishes, in its order.
SKILL_FILES: tuple[str, ...] = (
    "SKILL.md",
    "references/commands.md",
    "references/journal-policy.md",
)

REQUIRED_KEYS = frozenset({
    "action",
    "atomic_directory_publish",
    "destination",
    "files",
    "operation",
    "outcome",
    "product_version",
    "schema_version",
})
#: ``code`` is the only optional key, and only a refusal is allowed to carry it.
CODE_KEY = "code"

#: The exit codes the helper's CLI pairs with each disposition.  A document
#: that disagrees with the process that produced it is not admitted at all.
EXIT_REFUSED = 2
EXIT_SETTLED = 0

#: Every ``(action, outcome)`` pair ``remote_skill_install`` can emit, and the
#: status each one means here.  Read off the helper, not guessed: ``updated``
#: is as real an outcome as ``installed``, and ``noop`` carries its own.
STATUS_BY_DISPOSITION: dict[tuple[str, str], str] = {
    ("install", "planned"): "absent",
    ("update", "planned"): "outdated",
    ("noop", "noop"): "current",
    ("install", "installed"): "installed",
    ("update", "updated"): "updated",
    ("refuse", "refused"): "refused",
}
STATUS_UNKNOWN = "unknown"
SKILL_STATUSES = frozenset(set(STATUS_BY_DISPOSITION.values()) | {STATUS_UNKNOWN})

#: The two statuses an inspect may answer that leave something to install.
OFFERING_STATUSES = frozenset({"absent", "outdated"})

#: The published flow code for each admitted status, and for each refusal the
#: helper names.  This is the mapping the audit left unowned; it lives here.
FLOW_CODE_BY_STATUS: dict[str, str] = {
    "absent": "skill_absent",
    "outdated": "skill_outdated",
    "current": "skill_current",
    "installed": "skill_installed",
    "updated": "skill_updated",
    STATUS_UNKNOWN: "skill_unknown",
}
FLOW_CODE_BY_REFUSAL: dict[str, str] = {
    "APP_NOT_VERIFIED": "skill_refused_app_not_verified",
    "SKILL_DEST_FOREIGN": "skill_refused_destination_foreign",
    "SKILL_DEST_MODIFIED": "skill_refused_destination_modified",
}
FLOW_CODE_REFUSED = "skill_refused_failed"

#: Every code this module can publish, so the flow's own set can be checked
#: against one place rather than restated.
SKILL_FLOW_CODES = frozenset(
    set(FLOW_CODE_BY_STATUS.values())
    | set(FLOW_CODE_BY_REFUSAL.values())
    | {FLOW_CODE_REFUSED}
)

#: Bounded local codes for a channel that never produced an admissible answer.
#: They are evidence kept on the outcome; none of them is a helper refusal.
CHANNEL_UNAVAILABLE = "skill_channel_unavailable"
CHANNEL_LOST = "skill_channel_lost"
CHANNEL_UNBUILDABLE = "skill_channel_unbuildable"
DOCUMENT_UNRECOGNISED = "skill_document_unrecognised"
DOCUMENT_MISMATCHED = "skill_document_mismatched"
#: The closed set of those, so an unknown outcome can never carry an
#: unbounded string that a remote process chose.
CHANNEL_CODES = frozenset({
    CHANNEL_UNAVAILABLE,
    CHANNEL_LOST,
    CHANNEL_UNBUILDABLE,
    DOCUMENT_UNRECOGNISED,
    DOCUMENT_MISMATCHED,
})

MAX_CODE_LENGTH = 64
MAX_VERSION_LENGTH = 64
MAX_FILE_SIZE = 32 * 1024 * 1024
#: The helper writes prefixed digests, so a bare hex string is not one.
DIGEST_PREFIX = "sha256:"
_DIGEST_LENGTH = len(DIGEST_PREFIX) + 64
_DIGEST_CHARS = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class SkillFile:
    """One published destination-relative file, as the helper measured it."""

    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SkillOutcome:
    """What one helper run said, or why nothing it said could be believed.

    ``status`` is the admitted classification and ``action`` / ``outcome`` /
    ``code`` are the helper's own words, kept so a refusal can be reported as
    the refusal it is rather than as a generic failure.  ``mutating`` records
    whether ``--apply`` was actually sent, which is what separates "nothing was
    written" from "it is not known whether anything was written".
    """

    status: str = STATUS_UNKNOWN
    action: str = ""
    outcome: str = ""
    code: str = ""
    product_version: str | None = None
    files: tuple[SkillFile, ...] = field(default_factory=tuple)
    mutating: bool = False

    @property
    def offers_install(self) -> bool:
        """True only for an inspect that found real work to do."""

        return self.status in OFFERING_STATUSES and not self.mutating


class SkillPort(Protocol):
    """Read, and on an explicit second click write, the operator's Skill.

    Both calls are bound to the same verified application the update prepared;
    neither takes an operation identity, because this step issues no journalled
    mutation and reconciles by measuring ``$HOME`` again rather than by
    remembering a request.  Neither call raises: an unusable channel is an
    ``unknown`` outcome, which is a fact the screen can show.
    """

    def inspect_skill(self) -> SkillOutcome: ...
    def install_skill(self) -> SkillOutcome: ...


def skill_flow_code(outcome: SkillOutcome) -> str:
    """The one published flow code for one admitted outcome.

    Named and total: a refusal the helper spelled becomes that refusal's code,
    any other refusal becomes the generic one, and anything not admitted at all
    becomes ``skill_unknown``.  Nothing here can produce a success code from a
    document that was not admitted.
    """

    if outcome.status == "refused":
        return FLOW_CODE_BY_REFUSAL.get(outcome.code, FLOW_CODE_REFUSED)
    return FLOW_CODE_BY_STATUS.get(outcome.status, FLOW_CODE_BY_STATUS[STATUS_UNKNOWN])


def unknown_outcome(code: str, *, mutating: bool) -> SkillOutcome:
    """The outcome for a run that produced no admissible answer at all.

    ``code`` is one of this module's own local codes and never anything a
    remote process wrote, so anything else is reported as an unrecognised
    answer rather than repeated back.
    """

    local = code if code in CHANNEL_CODES else DOCUMENT_UNRECOGNISED
    return SkillOutcome(status=STATUS_UNKNOWN, code=local, mutating=mutating)


def admit_skill_outcome(
    document: object,
    returncode: object,
    *,
    expected_version: str,
    mutating: bool,
) -> SkillOutcome | None:
    """Admit one helper document, or ``None`` if it may not be believed.

    ``expected_version`` is the product version of the artifact this update
    prepared and verified.  A helper answering for a different build is not
    describing the application the operator just installed, so it is refused
    rather than shown.
    """

    if not _document_shape_admits(document):
        return None
    assert isinstance(document, Mapping)
    status = STATUS_BY_DISPOSITION.get(
        (str(document["action"]), str(document["outcome"]))
    )
    if status is None or not _exit_code_agrees(returncode, status):
        return None
    code = _admitted_code(document, status)
    if code is None:
        return None
    version = _admitted_version(document["product_version"])
    if version is None or version != expected_version:
        return None
    files = _admitted_files(document["files"])
    if files is None:
        return None
    return SkillOutcome(
        status=status,
        action=str(document["action"]),
        outcome=str(document["outcome"]),
        code=code,
        product_version=version,
        files=files,
        mutating=mutating,
    )


def _document_shape_admits(document: object) -> bool:
    """The fixed key set and every constant field the helper always writes."""

    if not isinstance(document, Mapping):
        return False
    keys = set(document)
    if keys != REQUIRED_KEYS and keys != REQUIRED_KEYS | {CODE_KEY}:
        return False
    return (
        document["schema_version"] == HELPER_SCHEMA_VERSION
        and type(document["schema_version"]) is int
        and document["operation"] == HELPER_OPERATION
        and document["destination"] == HELPER_DESTINATION
        and document["atomic_directory_publish"] is False
    )


def _exit_code_agrees(returncode: object, status: str) -> bool:
    """A helper that refused exits two; every other admitted answer exits zero."""

    expected = EXIT_REFUSED if status == "refused" else EXIT_SETTLED
    return type(returncode) is int and returncode == expected


def _admitted_code(document: Mapping[str, object], status: str) -> str | None:
    """A refusal carries exactly one bounded code; nothing else carries any."""

    present = CODE_KEY in document
    if status == "refused":
        if not present:
            return None
        return _bounded_code(document[CODE_KEY]) or None
    return None if present else ""


def _bounded_code(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CODE_LENGTH:
        return ""
    if not all(character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value):
        return ""
    return value


def _admitted_version(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_VERSION_LENGTH:
        return None
    if any(character < " " or character == "\x7f" for character in value):
        return None
    return value


def _admitted_files(value: object) -> tuple[SkillFile, ...] | None:
    """Exactly the three published files, each with a real digest and size."""

    if not isinstance(value, Mapping) or set(value) != set(SKILL_FILES):
        return None
    files: list[SkillFile] = []
    for name in SKILL_FILES:
        record = value[name]
        if not isinstance(record, Mapping) or set(record) != {"sha256", "size"}:
            return None
        digest = record["sha256"]
        size = record["size"]
        if not _is_digest(digest) or type(size) is not int or not 0 <= size <= MAX_FILE_SIZE:
            return None
        files.append(SkillFile(name=name, sha256=digest, size=size))
    return tuple(files)


def _is_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != _DIGEST_LENGTH:
        return False
    if not value.startswith(DIGEST_PREFIX):
        return False
    return all(character in _DIGEST_CHARS for character in value[len(DIGEST_PREFIX):])


__all__ = [
    "CHANNEL_CODES",
    "DIGEST_PREFIX",
    "CHANNEL_LOST",
    "CHANNEL_UNAVAILABLE",
    "CHANNEL_UNBUILDABLE",
    "DOCUMENT_MISMATCHED",
    "DOCUMENT_UNRECOGNISED",
    "FLOW_CODE_BY_REFUSAL",
    "FLOW_CODE_BY_STATUS",
    "FLOW_CODE_REFUSED",
    "HELPER_DESTINATION",
    "HELPER_OPERATION",
    "HELPER_SCHEMA_VERSION",
    "OFFERING_STATUSES",
    "SKILL_FILES",
    "SKILL_FLOW_CODES",
    "SKILL_STATUSES",
    "STATUS_BY_DISPOSITION",
    "STATUS_UNKNOWN",
    "SkillFile",
    "SkillOutcome",
    "SkillPort",
    "admit_skill_outcome",
    "skill_flow_code",
    "unknown_outcome",
]
