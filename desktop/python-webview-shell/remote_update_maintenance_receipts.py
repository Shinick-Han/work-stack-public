"""Durable operation receipts for one remote maintenance operation.

A maintenance operation is bound to durable evidence *before* it is allowed to
touch anything: the attempt receipt names the canonical workspace, the
operation kind, the operation UUID and, for a restore, the exact archive digest
that was selected.  The binding is published with the existing exclusive
publication helper, so an attempt receipt is created or it is not; it is never
replaced and never partially written.

Two receipts, never one mutable file:

* ``<operation>.attempt.json`` says an effect was authorised and may already
  have happened.  Finding it on a later invocation of the same operation is the
  signal that the completion of that effect is *unknown*, which is what stops a
  blind reissue of an attempted mutation.
* ``<operation>.completed.json`` says the supported maintenance call actually
  returned and its result verified.  It carries the content-free result that a
  later observation replays instead of guessing.

Nothing here takes the store writer lease, deletes a lock, or writes inside the
SSOT.  The receipt root is an operator-selected directory outside it.  Every
I/O or decode failure raises :class:`ReceiptUnavailable`, which the operation
layer reports as *unknown* rather than as a completed or refused effect, and no
raw exception text ever escapes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


def _shell_dir() -> str:
    source = globals().get("__file__")
    if type(source) is not str or not source:
        return ""
    try:
        return str(Path(source).resolve(strict=True).parent)
    except OSError:
        return ""


_SHELL_DIR = _shell_dir()
if _SHELL_DIR and _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from remote_receipt_io import ReceiptAlreadyExists  # noqa: E402
from remote_receipt_io import ReceiptPublicationUnavailable  # noqa: E402
from remote_receipt_io import publish_bytes_exclusive  # noqa: E402


RECEIPT_SCHEMA_VERSION = "remote-update-maintenance-receipt/1"
ATTEMPT_SUFFIX = ".attempt.json"
COMPLETED_SUFFIX = ".completed.json"
MAX_RECEIPT_BYTES = 64 * 1024

BINDING_KEYS = (
    "backup_digest",
    "backup_root",
    "kind",
    "operation_id",
    "source_operation_id",
    "workspace_dir",
    "workspace_id",
)

BOUND = "bound"
ALREADY_ATTEMPTED = "already_attempted"


class ReceiptUnavailable(Exception):
    """The receipt could not be read or published; completion stays unknown."""


class BindingMismatch(Exception):
    """A receipt for this operation UUID names a different operation."""


def attempt_path(state_root: Path, operation_id: str) -> Path:
    return Path(state_root) / (operation_id + ATTEMPT_SUFFIX)


def completed_path(state_root: Path, operation_id: str) -> Path:
    return Path(state_root) / (operation_id + COMPLETED_SUFFIX)


def encode(document: Mapping[str, Any]) -> bytes:
    """Encode one receipt document as sorted, newline-terminated UTF-8 JSON."""

    try:
        body = json.dumps(dict(document), ensure_ascii=False, sort_keys=True) + "\n"
    except (TypeError, ValueError) as error:
        raise ReceiptUnavailable("receipt document is not encodable") from error
    payload = body.encode("utf-8")
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ReceiptUnavailable("receipt document exceeds the bounded size")
    return payload


def read_document(path: Path) -> dict[str, Any] | None:
    """Read one receipt, or ``None`` when that receipt does not exist."""

    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_RECEIPT_BYTES:
            raise ReceiptUnavailable("receipt is larger than the bounded size")
        body = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ReceiptUnavailable("receipt could not be read") from error
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ReceiptUnavailable("receipt is not a readable document") from error
    if type(document) is not dict:
        raise ReceiptUnavailable("receipt is not a receipt document")
    return document


def binding_of(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: document.get(key) for key in BINDING_KEYS}


def assert_binding(document: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    """Refuse a receipt whose recorded identity is not this operation's."""

    if binding_of(document) != binding_of(binding):
        raise BindingMismatch("the stored receipt names a different operation")


def _prepare_root(state_root: Path) -> None:
    try:
        Path(state_root).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReceiptUnavailable("receipt root could not be prepared") from error


def _publish(path: Path, document: Mapping[str, Any]) -> bool:
    """Publish one receipt exclusively. ``False`` means it already existed."""

    try:
        publish_bytes_exclusive(path, encode(document))
    except ReceiptAlreadyExists:
        return False
    except ReceiptPublicationUnavailable as error:
        raise ReceiptUnavailable("receipt could not be published") from error
    except OSError as error:
        raise ReceiptUnavailable("receipt could not be published") from error
    return True


def _document(kind: str, binding: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt": kind,
    }
    document.update(binding_of(binding))
    document.update(extra)
    return document


def bind_attempt(state_root: Path, binding: Mapping[str, Any]) -> str:
    """Bind this operation durably before any effect is allowed to run.

    Returns ``BOUND`` when this invocation is the one that authorised the
    effect, and ``ALREADY_ATTEMPTED`` when a previous invocation of the same
    operation UUID already did.  ``ALREADY_ATTEMPTED`` never means the effect
    completed: whether it did is exactly what the caller must not guess.
    """

    _prepare_root(state_root)
    path = attempt_path(state_root, str(binding["operation_id"]))
    if _publish(path, _document("attempt", binding)):
        return BOUND
    existing = read_document(path)
    if existing is None:
        raise ReceiptUnavailable("the attempt receipt disappeared while binding")
    assert_binding(existing, binding)
    return ALREADY_ATTEMPTED


def read_attempt(state_root: Path, binding: Mapping[str, Any]) -> dict[str, Any] | None:
    document = read_document(attempt_path(state_root, str(binding["operation_id"])))
    if document is not None:
        assert_binding(document, binding)
    return document


def read_completed(state_root: Path, binding: Mapping[str, Any]) -> dict[str, Any] | None:
    document = read_document(completed_path(state_root, str(binding["operation_id"])))
    if document is not None:
        assert_binding(document, binding)
    return document


def complete(
    state_root: Path, binding: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    """Record the verified result of an operation that actually returned.

    Written only after the supported maintenance call returned and its result
    verified.  A completed receipt is never replaced: if one already exists for
    this operation UUID it must name the same operation, and its recorded
    result is the answer.
    """

    _prepare_root(state_root)
    path = completed_path(state_root, str(binding["operation_id"]))
    document = _document("completed", binding, result=dict(result))
    if _publish(path, document):
        return document
    existing = read_document(path)
    if existing is None:
        raise ReceiptUnavailable("the completed receipt disappeared while writing")
    assert_binding(existing, binding)
    return existing


def recorded_result(document: Mapping[str, Any]) -> dict[str, Any] | None:
    result = document.get("result")
    if type(result) is not dict:
        return None
    return dict(result)


def is_outside(candidate: Path, workspace_dir: Path) -> bool:
    """True when ``candidate`` is neither the SSOT directory nor inside it."""

    try:
        root = Path(candidate).resolve()
        workspace = Path(workspace_dir).resolve()
    except OSError:
        return False
    if root == workspace:
        return False
    try:
        common = os.path.commonpath([str(root), str(workspace)])
    except ValueError:
        return True
    return common != str(workspace)
