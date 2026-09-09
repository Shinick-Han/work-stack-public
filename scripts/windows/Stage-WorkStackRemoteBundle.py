"""Stage one already built Linux remote artifact into the Windows payload.

The Windows installer ships a Linux archive it did not build.  This puts the
one bounded link between those two builds in a single place: the chosen
archive and its chosen sidecar are admitted by the product's own
``remote_provision_artifact`` gate -- the same admission the desktop update
flow runs before any SSH -- and only then copied, byte for byte, to the
canonical ``<payload>/remote/`` name the host reads at update time.

Nothing here downloads, unpacks, re-signs or re-implements a digest check.
The archive is opaque bytes to this script.  Refusal is the default: an
unadmitted pair, a version that is not the version this Windows build ships,
a name that is not the canonical one, or an already populated destination all
fail packaging instead of publishing an installer with a mismatched payload.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

SCHEMA_VERSION = 1
VERSION_RE = re.compile(r"^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$")
REMOTE_DIRECTORY = "remote"


class StagingError(RuntimeError):
    """One refusal that must fail Windows packaging closed."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__("%s %s" % (code, detail))


def load_admission(source_root: Path):
    """Return the product's own artifact gate and remote installer identity.

    Imported from the source tree being packaged, so the rules applied here
    are exactly the rules the shipped desktop applies -- not a copy of them.
    """

    shell = source_root / "desktop" / "python-webview-shell"
    if not (shell / "remote_provision_artifact.py").is_file():
        raise StagingError("SOURCE", "the remote artifact gate is missing from %s" % shell)
    if str(shell) not in sys.path:
        sys.path.insert(0, str(shell))
    import remote_provision_artifact as artifact
    import remote_provision_installer as installer

    return artifact, installer


def require_version(value: str, what: str) -> str:
    if not VERSION_RE.match(value):
        raise StagingError(
            "VERSION", "%s is not a plain three-part release version: %r" % (what, value)
        )
    return value


def read_selected(path: Path, what: str) -> bytes:
    """Read one explicitly chosen local file. No URL, no directory walk."""

    if not path.is_file():
        raise StagingError("INPUT", "the selected %s does not exist: %s" % (what, path))
    return path.read_bytes()


def require_outside_payload(path: Path, payload: Path, what: str) -> None:
    """A source that already lives in the private payload is not an input."""

    if path == payload or payload in path.parents:
        raise StagingError("INPUT", "the selected %s is inside the payload: %s" % (what, path))


def admit_pair(artifact, archive_bytes: bytes, sidecar_bytes: bytes):
    try:
        return artifact.admit_artifact(archive_bytes, sidecar_bytes)
    except artifact.ArtifactError as error:
        raise StagingError("ADMISSION", "the selected pair was refused: %s" % error.detail) from None


def require_identity(selection, installer, expected_version: str) -> None:
    """The staged artifact must be this release, for this remote protocol."""

    require_version(selection.product_version, "the admitted product version")
    if selection.product_version != expected_version:
        raise StagingError(
            "VERSION",
            "the admitted artifact is %s but this Windows build is %s"
            % (selection.product_version, expected_version),
        )
    if selection.product_version != installer.PRODUCT:
        raise StagingError(
            "VERSION",
            "the admitted artifact is %s but the remote installer engine ships %s"
            % (selection.product_version, installer.PRODUCT),
        )
    if selection.protocol_version != installer.PROTOCOL:
        raise StagingError(
            "PROTOCOL",
            "the admitted artifact speaks remote protocol %d but this build supports %d"
            % (selection.protocol_version, installer.PROTOCOL),
        )


def canonical_names(selection, installer) -> tuple[str, str]:
    """Derive both destination names from admitted identity, never from input.

    The stem is rebuilt from the admitted product version and the frozen
    target id, so a hostile or merely wrong input filename cannot choose
    where these bytes land.
    """

    stem = "WorkStack-Linux-%s-%s" % (selection.product_version, installer.TARGET_ID)
    if "/" in stem or "\\" in stem or Path(stem).name != stem:
        raise StagingError("NAME", "the canonical name is not a single path segment: %r" % stem)
    archive_name = stem + ".zip"
    if archive_name != installer.ARCHIVE_NAME:
        raise StagingError(
            "NAME",
            "the canonical archive name %s is not the name the remote installer expects (%s)"
            % (archive_name, installer.ARCHIVE_NAME),
        )
    return archive_name, stem + ".json"


def prepare_destination(payload: Path, names: tuple[str, str]) -> tuple[Path, Path, Path]:
    """Create an empty payload/remote. An existing one is never overwritten."""

    remote = payload / REMOTE_DIRECTORY
    if remote.exists():
        raise StagingError("DESTINATION", "the payload already has a remote directory: %s" % remote)
    destinations = []
    for name in names:
        destination = remote / name
        if destination.parent != remote:
            raise StagingError(
                "DESTINATION", "refusing a destination outside %s: %s" % (remote, destination)
            )
        destinations.append(destination)
    remote.mkdir(parents=True)
    return remote, destinations[0], destinations[1]


def write_exactly(destination: Path, payload_bytes: bytes) -> None:
    """Create and write. ``xb`` refuses an existing file rather than replacing it."""

    with open(destination, "xb") as handle:
        handle.write(payload_bytes)


def require_copied_pair(artifact, selection, archive: Path, sidecar: Path):
    """Re-admit the bytes that are actually on disk, from disk.

    What ships is this copy, so this copy -- not the source that was read
    earlier -- is what the gate is asked about a second time.
    """

    copied = admit_pair(artifact, archive.read_bytes(), sidecar.read_bytes())
    same = (
        copied.archive_bytes == selection.archive_bytes
        and copied.sidecar_bytes == selection.sidecar_bytes
        and copied.digest == selection.digest
        and copied.manifest_digest == selection.manifest_digest
        and copied.product_version == selection.product_version
        and copied.protocol_version == selection.protocol_version
    )
    if not same:
        raise StagingError("STAGED", "the staged pair is not the pair that was admitted")
    return copied


def summary(selection, archive: Path, sidecar: Path, payload: Path) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "archive": archive.relative_to(payload).as_posix(),
        "sidecar": sidecar.relative_to(payload).as_posix(),
        "archive_size": len(selection.archive_bytes),
        "sidecar_size": len(selection.sidecar_bytes),
        "product_version": selection.product_version,
        "remote_protocol_version": selection.protocol_version,
        "artifact_digest": selection.digest,
        "artifact_manifest_sha256": selection.manifest_digest,
    }


def stage(
    source_root: Path,
    payload: Path,
    archive_path: Path,
    sidecar_path: Path,
    expected_version: str,
) -> dict:
    """Admit one pair and place it, unchanged, at its canonical payload name."""

    require_version(expected_version, "the Windows source version")
    if not payload.is_dir():
        raise StagingError("DESTINATION", "the payload root does not exist: %s" % payload)
    artifact, installer = load_admission(source_root)
    for path, what in ((archive_path, "archive"), (sidecar_path, "sidecar")):
        require_outside_payload(path, payload, what)
    if archive_path == sidecar_path:
        raise StagingError("INPUT", "the archive and the sidecar are the same file: %s" % archive_path)
    selection = admit_pair(
        artifact, read_selected(archive_path, "archive"), read_selected(sidecar_path, "sidecar")
    )
    require_identity(selection, installer, expected_version)
    names = canonical_names(selection, installer)
    remote, archive_destination, sidecar_destination = prepare_destination(payload, names)
    try:
        write_exactly(archive_destination, selection.archive_bytes)
        write_exactly(sidecar_destination, selection.sidecar_bytes)
        staged = require_copied_pair(artifact, selection, archive_destination, sidecar_destination)
    except BaseException:
        # Only the directory this call created is removed, and only from
        # inside the private payload.
        shutil.rmtree(remote, ignore_errors=True)
        raise
    return summary(staged, archive_destination, sidecar_destination, payload)


def resolve(value: str) -> Path:
    return Path(value).expanduser().absolute()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage an admitted Linux remote artifact into a Windows installer payload."
    )
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--payload", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--expect-version", required=True)
    options = parser.parse_args(argv)
    try:
        document = stage(
            resolve(options.source_root),
            resolve(options.payload),
            resolve(options.archive),
            resolve(options.sidecar),
            options.expect_version,
        )
    except StagingError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
