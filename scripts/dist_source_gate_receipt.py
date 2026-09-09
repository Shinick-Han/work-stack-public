#!/usr/bin/env python3
"""Receipt schema, closed-shape validation, digest folding, and receipt I/O.

Stdlib only. This module does not walk a live tree or run a build: it names and
checks the declared binding between source inputs and an admitted dist
manifest. Loaded as a sibling of ``dist_source_gate.py`` so file-path callers
keep working without a package import.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 2
DIGEST_TAG = "workstack-dist-source/2"
# Summarises one dist manifest so an admitted tree can cross a process boundary
# (the Windows packager reads it from this gate's JSON output) as a single value.
DIST_TREE_TAG = "workstack-dist-tree/2"
DIST_DIR = "frontend/dist"
ARTIFACT_DIR = ".artifacts"
RECEIPT_NAME = "dist-source-receipt.json"
REPAIR_COMMAND = "python scripts/release_gate.py refresh-dist --repo {root}"


class DistSourceGateError(Exception):
    def __init__(self, code: str, detail: str, *, preserved: str | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.preserved = preserved


def _repair(repo: Path) -> str:
    return REPAIR_COMMAND.format(root=Path(repo).as_posix())


def _is_reparse(path: Path) -> bool:
    """True for a symlink, a Windows junction, or any other reparse point."""

    try:
        status = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(status.st_mode):
        return True
    return bool(getattr(status, "st_reparse_tag", 0))


def receipt_path(repo: Path) -> Path:
    return Path(repo) / ARTIFACT_DIR / RECEIPT_NAME


def source_digest(entries: Sequence[Mapping[str, str]]) -> str:
    digest = hashlib.sha256()
    digest.update((DIGEST_TAG + "\n").encode("utf-8"))
    for entry in entries:
        digest.update(f"{entry['path']}\0{entry['sha256']}\n".encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def dist_digest(files: Sequence[Mapping[str, Any]]) -> str:
    """Summarise one dist manifest -- path, size and content digest of each file."""

    digest = hashlib.sha256()
    digest.update((DIST_TREE_TAG + "\n").encode("utf-8"))
    for entry in files:
        digest.update(f"{entry['path']}\0{entry['size']}\0{entry['sha256']}\n".encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def build_receipt(
    entries: Sequence[Mapping[str, str]], files: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_digest": source_digest(entries),
        "source_inputs": [dict(entry) for entry in entries],
        "dist": {"files": [dict(item) for item in files]},
    }


def write_receipt(path: Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(path)
    if _is_reparse(path):
        raise DistSourceGateError(
            "DIST_RECEIPT_INVALID", f"{path.as_posix()} is a symlink or junction"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return dict(receipt)


def _require_entry(entry: Any, fields: tuple[str, ...], label: str, code: str) -> dict[str, Any]:
    """Validate one declared manifest object: exact fields and typed values."""

    if not isinstance(entry, dict) or set(entry) != set(fields):
        raise DistSourceGateError(
            code, f"{label} entry must declare exactly {sorted(fields)}"
        )
    if not isinstance(entry["path"], str) or not entry["path"]:
        raise DistSourceGateError(code, f"{label} path must be a non-empty string")
    if not isinstance(entry["sha256"], str) or not entry["sha256"]:
        raise DistSourceGateError(code, f"{label} digest must be a non-empty string")
    if "size" in fields and (
        not isinstance(entry["size"], int) or isinstance(entry["size"], bool) or entry["size"] < 0
    ):
        raise DistSourceGateError(code, f"{label} size must be a non-negative integer")
    return dict(entry)


def _require_manifest(
    value: Any, fields: tuple[str, ...], label: str, code: str = "DIST_RECEIPT_INVALID"
) -> list[dict[str, Any]]:
    """Validate one declared manifest array: exact fields, typed, path-ordered.

    ``label`` names the array and ``code`` its refusal family: a receipt read
    from disk is invalid, while a manifest handed in by a caller leaves the
    staged tree unbound.
    """

    if not isinstance(value, list) or not value:
        raise DistSourceGateError(code, f"{label} must be a non-empty array")
    previous = b""
    collected: list[dict[str, Any]] = []
    for item in value:
        entry = _require_entry(item, fields, label, code)
        current = entry["path"].encode("utf-8")
        if current <= previous:
            raise DistSourceGateError(code, f"{label} must be unique and sorted by path")
        previous = current
        collected.append(entry)
    return collected


def read_receipt(path: Path) -> dict[str, Any]:
    """Load a receipt and validate every field this gate declares.

    ``source_inputs`` is a declared field of this schema, so it is required,
    shape-checked, and re-summarised: a receipt whose ``source_digest`` does not
    summarise its own ``source_inputs`` is refused rather than accepted.
    """

    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise DistSourceGateError(
            "DIST_RECEIPT_MISSING",
            f"{path.as_posix()} is absent, so nothing states which source built {DIST_DIR}",
        )
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as failure:
        raise DistSourceGateError(
            "DIST_RECEIPT_INVALID", f"{path.as_posix()}: {failure}"
        ) from failure
    if not isinstance(receipt, dict) or receipt.get("schema_version") != SCHEMA_VERSION:
        raise DistSourceGateError(
            "DIST_RECEIPT_INVALID", f"receipt must use schema_version {SCHEMA_VERSION}"
        )
    if set(receipt) != {"schema_version", "source_digest", "source_inputs", "dist"}:
        raise DistSourceGateError("DIST_RECEIPT_INVALID", "receipt declares unexpected fields")
    recorded = receipt.get("source_digest")
    if not isinstance(recorded, str) or not recorded.startswith("sha256:"):
        raise DistSourceGateError("DIST_RECEIPT_INVALID", "receipt has no source digest")
    inputs = _require_manifest(
        receipt.get("source_inputs"), ("path", "sha256"), "receipt source_inputs"
    )
    if source_digest(inputs) != recorded:
        raise DistSourceGateError(
            "DIST_RECEIPT_INVALID",
            "receipt source_digest does not summarise its own source_inputs",
        )
    dist = receipt.get("dist")
    if not isinstance(dist, dict) or set(dist) != {"files"}:
        raise DistSourceGateError("DIST_RECEIPT_INVALID", "receipt has no dist file manifest")
    _require_manifest(dist.get("files"), ("path", "size", "sha256"), "receipt dist.files")
    return receipt


def _first_differences(
    recorded: Sequence[Mapping[str, str]], actual: Sequence[Mapping[str, str]]
) -> str:
    before = {entry["path"]: entry["sha256"] for entry in recorded}
    after = {entry["path"]: entry["sha256"] for entry in actual}
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    return ", ".join(changed[:3]) + (f" and {len(changed) - 3} more" if len(changed) > 3 else "")


def _admitted_dist_files(admitted: Mapping[str, Any]) -> list[dict[str, Any]]:
    files = admitted.get("dist_files") if isinstance(admitted, Mapping) else None
    if files is None:
        raise DistSourceGateError(
            "DIST_STAGED_UNBOUND",
            "no admitted dist manifest was supplied, so the staged tree cannot be bound to the "
            "gate result",
        )
    return _require_manifest(
        files, ("path", "size", "sha256"), "the admitted dist manifest", "DIST_STAGED_UNBOUND"
    )
