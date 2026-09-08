"""Pure builder for the remote installer stdin payload.

``python -I -B -`` consumes standard input until EOF, so framed binary data
cannot follow the program text on the same stream. This module therefore emits
one complete ASCII Python program that carries every byte the remote install
needs as base64 literals: the two trusted checked-in installer modules plus the
caller-supplied archive and sidecar.

Base64 inflates the transferred bytes by roughly 4/3 and the remote interpreter
holds both the encoded literal and the decoded bytes in memory. This is a
size-for-simplicity trade, not streaming and not zero-copy.

Import is effect-free: no filesystem, environment, network, process, or argv
read runs at import. The sibling module sources are read only when
``build_installer_payload_source`` is invoked, and only after the caller-supplied
bytes have been validated.

Scope: this module builds source text. It never executes it, never launches a
process, never touches SSH, credentials, or the home profile, and never accepts
arbitrary source, paths, module names, templates, or callbacks. Admission of the
archive stays entirely with the installer engine; the checks here are type and
size only.
"""

from __future__ import annotations

import base64
import os
import re


MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_SIDECAR_BYTES = 16 * 1024
MAX_MODULE_BYTES = 128 * 1024
BOOTSTRAP_BUDGET_BYTES = 8192
CHUNK_CHARS = 4096

LEAF_MODULE_NAME = "remote_provision_installer_linux"
ADMISSION_MODULE_NAME = "remote_provision_installer"
LEAF_MODULE_FILENAME = "remote_provision_installer_linux.py"
ADMISSION_MODULE_FILENAME = "remote_provision_installer.py"

INVALID_INPUT = "INVALID_PAYLOAD_INPUT"
SOURCE_UNAVAILABLE = "PAYLOAD_SOURCE_UNAVAILABLE"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"

_MESSAGES = {
    INVALID_INPUT: "installer payload inputs must be bytes within the fixed caps",
    SOURCE_UNAVAILABLE: "a checked-in installer module could not be read within its bound",
    PAYLOAD_TOO_LARGE: "generated installer payload exceeds the derived source bound",
}

_BASE64_RE = re.compile(r"\A[A-Za-z0-9+/]*={0,2}\Z")
_SHELL_DIR = os.path.dirname(os.path.abspath(__file__))

_PROLOGUE = '''import base64
import sys
import types

_LEAF_NAME = "remote_provision_installer_linux"
_MAIN_NAME = "remote_provision_installer"
_REFUSED = b'{"schema_version":1,"outcome":"refused","code":"REMOTE_INSTALL_FAILED"}\\n'
'''

_EPILOGUE = '''

def _load(name, encoded):
    module = types.ModuleType(name)
    module.__file__ = "<" + name + ">"
    module.__loader__ = None
    module.__spec__ = None
    sys.modules[name] = module
    exec(compile(base64.b64decode(encoded), "<" + name + ">", "exec"), module.__dict__)
    return module


def _main():
    _load(_LEAF_NAME, _LEAF_B64)
    module = _load(_MAIN_NAME, _MAIN_B64)
    archive = base64.b64decode(_ARCHIVE_B64)
    sidecar = base64.b64decode(_SIDECAR_B64)
    return int(module.__dict__["installer_main"](sys.argv[1:], archive, sidecar))


try:
    _status = _main()
except SystemExit:
    raise
except BaseException:
    try:
        sys.stderr.buffer.write(_REFUSED)
        sys.stderr.buffer.flush()
    except OSError:
        pass
    _status = 2
sys.exit(_status)
'''


class PayloadError(RuntimeError):
    """Sanitized installer payload build failure.

    Carries a stable code and a fixed message. Source text, filesystem paths,
    and raw exception detail are never propagated to the caller.
    """

    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code


def _encoded_bound(raw_bytes: int) -> int:
    """Return the exact base64 character count for ``raw_bytes`` input bytes."""

    return ((raw_bytes + 2) // 3) * 4


def _literal_bound(raw_bytes: int) -> int:
    """Return the worst-case source bytes a base64 literal block occupies."""

    encoded = _encoded_bound(raw_bytes)
    lines = max(1, (encoded + CHUNK_CHARS - 1) // CHUNK_CHARS)
    # Each emitted line adds two quotes and one newline around its chunk.
    return encoded + lines * 3


MAX_PAYLOAD_BYTES = (
    BOOTSTRAP_BUDGET_BYTES
    + _literal_bound(MAX_MODULE_BYTES) * 2
    + _literal_bound(MAX_ARCHIVE_BYTES)
    + _literal_bound(MAX_SIDECAR_BYTES)
)


def _checked_bytes(value: object, maximum: int) -> bytes:
    if type(value) is not bytes:
        raise PayloadError(INVALID_INPUT)
    if len(value) > maximum:
        raise PayloadError(INVALID_INPUT)
    return value


def _read_module_source(filename: str) -> bytes:
    """Read one fixed checked-in sibling module within its bound."""

    path = os.path.join(_SHELL_DIR, filename)
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_MODULE_BYTES + 1)
    except OSError:
        raise PayloadError(SOURCE_UNAVAILABLE) from None
    if not payload or len(payload) > MAX_MODULE_BYTES:
        raise PayloadError(SOURCE_UNAVAILABLE)
    return payload


def _literal(name: str, payload: bytes) -> str:
    """Render ``payload`` as a base64 string literal assignment.

    Only characters from the base64 alphabet reach the generated source, so
    quotes, newlines, backslashes, and code-shaped bytes in ``payload`` cannot
    terminate the literal or become executable text.
    """

    encoded = base64.b64encode(payload).decode("ascii")
    if _BASE64_RE.fullmatch(encoded) is None:
        raise PayloadError(SOURCE_UNAVAILABLE)
    chunks = [encoded[start : start + CHUNK_CHARS] for start in range(0, len(encoded), CHUNK_CHARS)]
    if not chunks:
        chunks = [""]
    body = "".join('"%s"\n' % chunk for chunk in chunks)
    return "%s = (\n%s)\n" % (name, body)


def build_installer_payload_source(*, archive_bytes: bytes, sidecar_bytes: bytes) -> bytes:
    """Build the complete ASCII installer program for ``python -I -B -`` stdin.

    The generated program decodes the two fixed installer modules in memory,
    registers the leaf under its required ``remote_provision_installer_linux``
    identity, loads the admission module, and calls
    ``installer_main(sys.argv[1:], archive_bytes, sidecar_bytes)``.

    ``archive_bytes`` and ``sidecar_bytes`` are checked for type and size only.
    Nothing here asserts that the archive is genuine, and the embedded bytes
    carry no provenance or authenticity claim; the installer engine remains the
    sole admission authority.
    """

    archive = _checked_bytes(archive_bytes, MAX_ARCHIVE_BYTES)
    sidecar = _checked_bytes(sidecar_bytes, MAX_SIDECAR_BYTES)
    leaf_source = _read_module_source(LEAF_MODULE_FILENAME)
    admission_source = _read_module_source(ADMISSION_MODULE_FILENAME)
    source = "".join(
        (
            _PROLOGUE,
            _literal("_LEAF_B64", leaf_source),
            _literal("_MAIN_B64", admission_source),
            _literal("_ARCHIVE_B64", archive),
            _literal("_SIDECAR_B64", sidecar),
            _EPILOGUE,
        )
    )
    try:
        encoded = source.encode("ascii")
    except UnicodeEncodeError:
        raise PayloadError(SOURCE_UNAVAILABLE) from None
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise PayloadError(PAYLOAD_TOO_LARGE)
    return encoded
