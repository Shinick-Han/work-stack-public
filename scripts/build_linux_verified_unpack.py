#!/usr/bin/env python3
"""Build a standalone verified-unpack helper from the checkout identity.

Embeds the current installer admission modules and the reviewed
remote_verified_unpack place/inspect/verify implementation. Pins
archive/sidecar hashes from the named bundle. Never overwrites published
1.0.13 release assets or remote_provision_installer*.py. The generated
tool still creates only an absent directory and does not rename, activate,
or access SSOT.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
LEAF_NAME = "remote_provision_installer_linux"
ADMISSION_NAME = "remote_provision_installer"
UNPACK_NAME = "remote_verified_unpack"
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_SIDECAR = 16 * 1024
MAX_MODULE = 256 * 1024
FROZEN_MARKERS = (
    "workstack-linux-1.0.13-distribution",
    "workstack-linux-1.0.13-unpack-published-verify",
)


class UnpackBuildError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_bounded(path: Path, limit: int) -> bytes:
    data = path.read_bytes()
    if not data or len(data) > limit:
        raise UnpackBuildError("SOURCE_UNAVAILABLE")
    return data


def frozen_release_asset(path: Path) -> bool:
    parts = {part.lower() for part in path.resolve().parts}
    return any(marker in parts for marker in FROZEN_MARKERS)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _hex_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_shell():
    if str(SHELL) not in sys.path:
        sys.path.insert(0, str(SHELL))
    from remote_provision_installer import PRODUCT
    from remote_provision_installer import PROTOCOL
    from remote_provision_installer import ARCHIVE_NAME
    from remote_provision_installer import _admit_artifact

    return {
        "PRODUCT": PRODUCT,
        "PROTOCOL": PROTOCOL,
        "ARCHIVE_NAME": ARCHIVE_NAME,
        "admit": _admit_artifact,
    }


def render_verified_unpack_script(archive_bytes: bytes, sidecar_bytes: bytes) -> tuple[str, dict[str, object]]:
    if type(archive_bytes) is not bytes or not archive_bytes or len(archive_bytes) > MAX_ARCHIVE:
        raise UnpackBuildError("REMOTE_ARTIFACT_INVALID")
    if type(sidecar_bytes) is not bytes or not sidecar_bytes or len(sidecar_bytes) > MAX_SIDECAR:
        raise UnpackBuildError("REMOTE_ARTIFACT_INVALID")
    engine = load_shell()
    admitted = engine["admit"](archive_bytes, sidecar_bytes)
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    product = engine["PRODUCT"]
    protocol = engine["PROTOCOL"]
    source_commit = sidecar["source_commit"]
    archive_sha256 = _hex_digest(archive_bytes)
    sidecar_sha256 = _hex_digest(sidecar_bytes)
    linux_b64 = _b64(_read_bounded(SHELL / (LEAF_NAME + ".py"), MAX_MODULE))
    admission_b64 = _b64(_read_bounded(SHELL / (ADMISSION_NAME + ".py"), MAX_MODULE))
    unpack_b64 = _b64(_read_bounded(SHELL / (UNPACK_NAME + ".py"), MAX_MODULE))
    identity = {
        "archive_name": engine["ARCHIVE_NAME"],
        "archive_sha256": archive_sha256,
        "artifact_digest": admitted["digest"],
        "artifact_manifest_sha256": admitted["manifest_digest"],
        "files": len(admitted["files"]),
        "product_version": product,
        "remote_protocol_version": protocol,
        "sidecar_sha256": sidecar_sha256,
        "source_commit": source_commit,
        "tool": "workstack-verified-unpack/1",
    }
    source = _SCRIPT_TEMPLATE % {
        "admission_b64": admission_b64,
        "archive_sha256": archive_sha256,
        "linux_b64": linux_b64,
        "product": product,
        "protocol": protocol,
        "sidecar_sha256": sidecar_sha256,
        "source_commit": source_commit,
        "dist_name": engine["ARCHIVE_NAME"],
        "unpack_b64": unpack_b64,
    }
    return source, identity


def write_verified_unpack_script(archive_bytes: bytes, sidecar_bytes: bytes, output: Path) -> dict[str, object]:
    if frozen_release_asset(output):
        raise UnpackBuildError("FROZEN_RELEASE_ASSET")
    if output.exists():
        raise UnpackBuildError("OUTPUT_EXISTS")
    source, identity = render_verified_unpack_script(archive_bytes, sidecar_bytes)
    compile(source, str(output), "exec")
    output.write_text(source, encoding="utf-8", newline="\n")
    identity["output"] = output.name
    return identity


_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""Work Stack %(product)s verified bundle unpacker (MIT).
Generated from checkout identity; NOT the transactional provision installer.
product_version=%(product)s remote_protocol_version=%(protocol)s
source_commit=%(source_commit)s dist=%(dist_name)s
archive_sha256=%(archive_sha256)s sidecar_sha256=%(sidecar_sha256)s
Composes the reviewed remote_verified_unpack place/inspect/verify state
machine. New-directory manual deployment. No renameat2, atomic directory
publish, SSH, pip, npm or SSOT access. Failed/interrupted directories must
never be activated. Artifact validation reuses the checkout installer source
bundled below.
"""
import argparse, base64, hashlib, json, sys, types
from pathlib import Path
sys.dont_write_bytecode = True
ARCHIVE_SHA256 = %(archive_sha256)r
SIDECAR_SHA256 = %(sidecar_sha256)r
MODULES = (
    ('remote_provision_installer_linux', %(linux_b64)r),
    ('remote_provision_installer', %(admission_b64)r),
    ('remote_verified_unpack', %(unpack_b64)r),
)

def load_engine():
    for name, encoded in MODULES:
        module = types.ModuleType(name)
        sys.modules[name] = module
        exec(compile(base64.b64decode(encoded), '<' + name + '>', 'exec'), module.__dict__)
    return sys.modules['remote_verified_unpack']

def read_checked(path, limit, digest):
    with Path(path).open('rb') as stream:
        data = stream.read(limit + 1)
    if not data or len(data) > limit or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('RELEASE_FILE_MISMATCH')
    return data

def main(argv=None):
    unpack = load_engine()
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('operation', nargs='?', default='place', choices=('place', 'inspect', 'verify'))
    parser.add_argument('--archive')
    parser.add_argument('--sidecar')
    parser.add_argument('--app-dir', required=True, help='New absolute app path; existing parent required')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'inspect':
            result = unpack.inspect_unpack_target(args.app_dir)
        elif args.operation == 'verify':
            if type(args.archive) is not str or type(args.sidecar) is not str:
                result = unpack._not_ready('REMOTE_ARTIFACT_INVALID', unpack.PLACEMENT_ABSENT)
            else:
                archive = read_checked(args.archive, 64 * 1024 * 1024, ARCHIVE_SHA256)
                sidecar = read_checked(args.sidecar, 16 * 1024, SIDECAR_SHA256)
                result = unpack.verify_unpack_identity(args.app_dir, archive, sidecar)
        else:
            if type(args.archive) is not str or type(args.sidecar) is not str:
                result = unpack._not_ready('REMOTE_ARTIFACT_INVALID', unpack.PLACEMENT_ABSENT)
            else:
                archive = read_checked(args.archive, 64 * 1024 * 1024, ARCHIVE_SHA256)
                sidecar = read_checked(args.sidecar, 16 * 1024, SIDECAR_SHA256)
                result = unpack.place_verified_unpack(
                    archive_bytes=archive,
                    sidecar_bytes=sidecar,
                    app_dir=args.app_dir,
                )
    except ValueError as error:
        result = unpack._not_ready(str(error), unpack.PLACEMENT_ABSENT)
    except OSError as error:
        result = unpack._not_ready(
            'FILESYSTEM_ERROR_ERRNO_' + str(error.errno),
            unpack.PLACEMENT_UNKNOWN,
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get('outcome') == unpack.OUTCOME_UNPACKED else 2

if __name__ == '__main__':
    raise SystemExit(main())
'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--output", required=True, help="New path outside frozen 1.0.13 release trees")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    archive = _read_bounded(Path(args.archive), MAX_ARCHIVE)
    sidecar = _read_bounded(Path(args.sidecar), MAX_SIDECAR)
    identity = write_verified_unpack_script(archive, sidecar, Path(args.output))
    print(json.dumps(identity, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UnpackBuildError as error:
        print(json.dumps({"outcome": "not_ready", "code": error.code}, sort_keys=True))
        raise SystemExit(2)
