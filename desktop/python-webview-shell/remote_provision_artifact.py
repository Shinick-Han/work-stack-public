"""Bind one chosen archive to one chosen sidecar, locally, before any SSH.

This is admission, not authentication.  Nothing here claims an artifact is
genuine: the remote installer engine remains the authority and re-derives all
of this from the bytes it actually receives.  What this does is refuse, on
this machine and before a process exists, a sidecar that does not describe
exactly the archive that was selected.

The strict JSON reader lives here too, because the same rules apply wherever
this driver admits a document: one object, no duplicate keys, no ``NaN`` or
``Infinity``, and a bounded size.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from remote_provision_evidence import DIGEST_PREFIX  # noqa: E402
from remote_provision_installer import MAX_ARCHIVE, MAX_SIDECAR  # noqa: E402


class ArtifactError(RuntimeError):
    """One selected archive and sidecar that do not describe each other."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class ArtifactSelection:
    """One explicitly chosen archive plus its explicitly chosen sidecar."""

    archive_bytes: bytes
    sidecar_bytes: bytes
    product_version: str
    protocol_version: int
    digest: str
    manifest_digest: str


def _unique_object(pairs: list[tuple[object, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in document:
            raise ValueError("invalid JSON object")
        document[key] = value
    return document


def _reject_constant(value: str) -> object:
    raise ValueError("invalid JSON constant")


def load_json_object(payload: bytes) -> dict[str, object]:
    """Read exactly one strict JSON object, or raise ``ValueError``."""

    value = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if type(value) is not dict:
        raise ValueError("not a JSON object")
    return value


def _admit_bytes(value: object, maximum: int, what: str) -> bytes:
    if type(value) is not bytes or not value:
        raise ArtifactError(f"{what} bytes are required")
    if len(value) > maximum:
        raise ArtifactError(f"{what} exceeds the installer bound")
    return value


def _admit_sidecar_archive(sidecar: dict[str, object], archive_bytes: bytes) -> str:
    """Require the sidecar to describe exactly the archive that was selected."""

    digest = DIGEST_PREFIX + hashlib.sha256(archive_bytes).hexdigest()
    archive = sidecar.get("archive")
    if type(archive) is not dict:
        raise ArtifactError("sidecar does not describe an archive")
    if archive.get("sha256") != digest:
        raise ArtifactError("sidecar digest does not match the selected archive")
    size = archive.get("size")
    if type(size) is not int or isinstance(size, bool) or size != len(archive_bytes):
        raise ArtifactError("sidecar size does not match the selected archive")
    return digest


def _admit_sidecar_identity(sidecar: dict[str, object]) -> tuple[str, int, str]:
    product = sidecar.get("product_version")
    if type(product) is not str or not product:
        raise ArtifactError("sidecar product_version is invalid")
    protocol = sidecar.get("remote_protocol_version")
    if type(protocol) is not int or isinstance(protocol, bool) or protocol < 0:
        raise ArtifactError("sidecar protocol version is invalid")
    manifest = sidecar.get("artifact_manifest_sha256")
    if type(manifest) is not str or not manifest.startswith(DIGEST_PREFIX):
        raise ArtifactError("sidecar manifest digest is invalid")
    return product, protocol, manifest


def admit_artifact(archive_bytes: object, sidecar_bytes: object) -> ArtifactSelection:
    """Bind one archive to one sidecar that already describes exactly it.

    The sidecar is the only source of the product version, protocol version,
    and manifest digest used downstream, and it must match this archive by
    both size and SHA-256.
    """

    archive = _admit_bytes(archive_bytes, MAX_ARCHIVE, "archive")
    encoded = _admit_bytes(sidecar_bytes, MAX_SIDECAR, "sidecar")
    try:
        sidecar = load_json_object(encoded)
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        raise ArtifactError("sidecar is not one JSON object") from None
    digest = _admit_sidecar_archive(sidecar, archive)
    product, protocol, manifest = _admit_sidecar_identity(sidecar)
    return ArtifactSelection(
        archive_bytes=archive,
        sidecar_bytes=encoded,
        product_version=product,
        protocol_version=protocol,
        digest=digest,
        manifest_digest=manifest,
    )


__all__ = [
    "ArtifactError", "ArtifactSelection", "admit_artifact", "load_json_object",
]
