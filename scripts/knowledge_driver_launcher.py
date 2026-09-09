"""Explicit pilot launcher: one owner server with pinned knowledge drivers.

Work Stack's execution route only runs a driver an *embedding process* pinned
(``create_server(..., knowledge_drivers=...)``); the shipped CLI constructs no
registry, and the default -- no driver at all -- stays the released behaviour.
This script is that embedding process for the pilot, and nothing else: it reads
one operator-authored registry file, cross-checks each declared driver against
that driver's own configuration, builds the registry, and serves it on the
loopback interface until the operator interrupts it.

**It is a source-checkout pilot, not an installed integration.** The pinned
``command`` is an absolute interpreter plus ``-m``, and an absolute
``PYTHONPATH`` is how the child finds this checkout. That argv does not survive
packaging or remote provisioning; a console script or a frozen executable is a
separate lane, and this file does not pretend otherwise.

**Trusted operator input, not browser input.** ``--drivers-config`` and every
path inside it are things the person running this command typed. No value here
comes from a request, a page or an answer, nothing is ever run through a shell,
and there is no ambient discovery: no default registry path, no default data
directory, no environment fallback, and no inheritance of this process'
environment into a child. Each child sees exactly the mapping the operator
declared.

**This process never reads an API key.** The registry file holds no key and no
key *variable*: a driver's credential lives in a separate file that only the
child opens (``integrations/opendocuments/driver_config.load_driver_key``).
What this launcher reads is the nonsecret driver document, so it can prove --
before it takes a store lease -- that the alias and upstream workspace the
operator registered are the ones that driver actually serves. The honest claim
is exactly that: *the launcher and the owner do not read the key.* The bytes
still belong to a file the same OS user can read, and this is not a sandbox.

**The pilot never adopts a live store.** ``--data-dir`` is required, absolute
and must be empty or absent: this experiment creates its own store, and it will
not open, migrate or reuse a directory that already holds work. The server
binds ``127.0.0.1`` and nothing else -- the host is not an option.

``--check-config`` admits the registry, every driver document, every catalog and
every origin, then exits. It opens no key file, spawns no child, binds no
socket, takes no store lease and touches no data directory.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Mapping

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from integrations.opendocuments.driver_config import (  # noqa: E402
    DriverConfigError,
    OperatorDriverConfig,
    load_operator_driver_config,
)
from integrations.opendocuments.driver_main import (  # noqa: E402
    CONFIG_ENVIRONMENT_VARIABLE,
)
from workstack.knowledge_execution_runtime import (  # noqa: E402
    MAX_DRIVERS,
    KnowledgeDriverBinding,
    KnowledgeDriverConfigurationError,
    admit_drivers,
)
from workstack.knowledge_request import (  # noqa: E402
    KnowledgeRequestError,
    canonical_uuid,
    decode_strict_json,
)
from workstack.server import create_server  # noqa: E402
from workstack.service import WorkStack  # noqa: E402
from workstack.store import Store  # noqa: E402

__all__ = [
    "ALLOWED_ENVIRONMENT_NAMES",
    "DRIVERS_CONFIG_FIELDS",
    "DRIVERS_CONFIG_SCHEMA",
    "DRIVER_ENTRY_FIELDS",
    "LAUNCHER_CODES",
    "LOOPBACK_HOST",
    "MAX_CONFIG_BYTES",
    "MAX_ENVIRONMENT_ENTRIES",
    "LauncherError",
    "build_driver_registry",
    "main",
]

#: The closed registry document this launcher reads.
DRIVERS_CONFIG_SCHEMA = "workstack.knowledge-drivers.v1"
DRIVERS_CONFIG_FIELDS = frozenset({"schema", "drivers"})
DRIVER_ENTRY_FIELDS = frozenset(
    {"alias", "upstream_workspace_uid", "command", "environment"}
)

#: The registry file's byte bound, on real UTF-8 octets.
MAX_CONFIG_BYTES = 64 * 1024

#: An explicit child environment is short by construction.
MAX_ENVIRONMENT_ENTRIES = 32
MAX_ENVIRONMENT_NAME_CHARS = 128
MAX_ENVIRONMENT_VALUE_CHARS = 4096

#: The only interface this pilot binds. It is not an option.
LOOPBACK_HOST = "127.0.0.1"

#: The closed set of variable names a pilot driver may be given. It is the
#: runtime minimum -- the configuration path, the import path, the two Windows
#: system roots, the search path, the temporary directories and the locale --
#: and an unknown name is refused rather than passed on, so this file cannot
#: accidentally become a place a credential travels. It is *pilot launcher
#: policy*, not a change to the generic ``KnowledgeDriverBinding``, which still
#: accepts whatever mapping its own embedding process states. The optional
#: names match case-insensitively; ``WORKSTACK_OD_DRIVER_CONFIG`` matches only
#: as spelled here -- see :func:`_admitted_environment_name`.
ALLOWED_ENVIRONMENT_NAMES = frozenset(
    {
        "WORKSTACK_OD_DRIVER_CONFIG",
        "PYTHONPATH",
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
    }
)

#: Every code this launcher prints. Closed, so a refusal never carries a path,
#: a file's content, or an interpreter's exception text.
LAUNCHER_CODES = frozenset(
    {
        "invalid_drivers_config_path",
        "drivers_config_unreadable",
        "invalid_drivers_config",
        "unsupported_drivers_config_schema",
        "invalid_driver_entry",
        "duplicate_driver_alias",
        "invalid_driver_environment",
        "missing_driver_config_variable",
        "unknown_driver_environment_name",
        "duplicate_driver_environment_name",
        "invalid_driver_config",
        "driver_config_mismatch",
        "invalid_driver_registry",
        "invalid_driver_alias",
        "invalid_driver_binding",
        "invalid_driver_command",
        "invalid_driver_upstream_workspace_uid",
        "driver_registry_full",
        "invalid_data_dir",
        "data_dir_not_empty",
        "invalid_port",
    }
)

_CONTROL = frozenset(range(0, 32)) | {127}
_MAX_PATH_CHARS = 4096


class LauncherError(ValueError):
    """Closed launcher refusal: one code from :data:`LAUNCHER_CODES`."""

    def __init__(self, code: str) -> None:
        if code not in LAUNCHER_CODES:
            code = "invalid_drivers_config"
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


# -- the registry ---------------------------------------------------------


def build_driver_registry(path: Any) -> Mapping[str, KnowledgeDriverBinding]:
    """Read the operator's registry and return the admitted driver mapping.

    Every declared driver's own nonsecret document is read here too, through
    the shared loader, and its alias and upstream workspace must match what the
    registry says. That cross-check happens *before* a store lease or a socket
    exists, so a registry that disagrees with a driver refuses to start a server
    instead of failing later as an unexplained ``driver_outcome_unknown``.

    No key file is opened, no child is started and no network is touched.
    """

    document = _read_registry(path)
    entries = document["drivers"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_DRIVERS:
        raise LauncherError("invalid_driver_entry")
    bindings: dict[str, KnowledgeDriverBinding] = {}
    for entry in entries:
        alias, binding = _admitted_entry(entry)
        if alias in bindings:
            raise LauncherError("duplicate_driver_alias")
        bindings[alias] = binding
    try:
        # The owner's own registry admission -- argv shape, environment shape,
        # alias grammar and canonical upstream UUID -- run here rather than
        # restated, so what this launcher accepts is exactly what the server
        # will accept a moment later.
        return admit_drivers(bindings)
    except KnowledgeDriverConfigurationError as error:
        raise LauncherError(error.code) from error


def _read_registry(path: Any) -> dict[str, Any]:
    absolute = _admitted_path(path, "invalid_drivers_config_path")
    try:
        with open(absolute, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise LauncherError("drivers_config_unreadable") from error
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise LauncherError("drivers_config_unreadable")
    try:
        document = decode_strict_json(raw, maximum_bytes=MAX_CONFIG_BYTES)
    except KnowledgeRequestError as error:
        raise LauncherError("invalid_drivers_config") from error
    if not isinstance(document, dict) or set(document) != DRIVERS_CONFIG_FIELDS:
        raise LauncherError("invalid_drivers_config")
    if document["schema"] != DRIVERS_CONFIG_SCHEMA:
        raise LauncherError("unsupported_drivers_config_schema")
    return document


def _admitted_entry(entry: Any) -> tuple[str, KnowledgeDriverBinding]:
    """One registry row, cross-checked against the driver's own document."""

    if not isinstance(entry, dict) or set(entry) != DRIVER_ENTRY_FIELDS:
        raise LauncherError("invalid_driver_entry")
    alias = entry["alias"]
    if not isinstance(alias, str) or not alias:
        raise LauncherError("invalid_driver_alias")
    try:
        upstream = canonical_uuid(entry["upstream_workspace_uid"], "upstream")
    except KnowledgeRequestError as error:
        raise LauncherError("invalid_driver_upstream_workspace_uid") from error
    environment = _admitted_environment(entry["environment"])
    _require_matching_driver_config(environment, alias, upstream)
    command = entry["command"]
    if not isinstance(command, list):
        raise LauncherError("invalid_driver_command")
    return alias, KnowledgeDriverBinding(
        upstream_workspace_uid=upstream,
        command=tuple(command),
        environment=environment,
    )


def _admitted_environment(environment: Any) -> dict[str, str]:
    """The exact mapping one child will see: closed names, operator values.

    Nothing is inherited -- this process never merges :data:`os.environ` -- and
    nothing outside :data:`ALLOWED_ENVIRONMENT_NAMES` is passed on, so a
    credential cannot ride to the child in a variable this file declares.
    Names collide case-insensitively because that is how *some* operating
    systems resolve them, and the required configuration variable is admitted
    only under the exact spelling the child looks up, because others do not.
    """

    if not isinstance(environment, dict) or len(environment) > MAX_ENVIRONMENT_ENTRIES:
        raise LauncherError("invalid_driver_environment")
    admitted: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in environment.items():
        folded = _admitted_environment_name(name)
        if folded in seen:
            raise LauncherError("duplicate_driver_environment_name")
        seen.add(folded)
        if (
            not isinstance(value, str)
            or len(value) > MAX_ENVIRONMENT_VALUE_CHARS
            or any(ord(character) in _CONTROL for character in value)
        ):
            raise LauncherError("invalid_driver_environment")
        admitted[name] = value
    return admitted


def _admitted_environment_name(name: Any) -> str:
    """Return the case-folded name, or refuse one this pilot will not pass on.

    Membership in the closed set is decided case-insensitively, because the
    optional names are the operating system's own and Windows spells one of
    them ``SystemRoot``. The **required** configuration variable is different:
    the child reads it with a single exact
    ``os.environ.get(CONFIG_ENVIRONMENT_VARIABLE)``, and on a case-sensitive
    system a registry that spelled it ``workstack_od_driver_config`` would be
    admitted here, cross-checked against a document this launcher can read, and
    then leave the child unable to find any configuration at all -- an attempt
    spent on ``driver_outcome_unknown`` for a file that was right the whole
    time. So a case variant of that one name is *refused*, not normalised: this
    launcher does not quietly rewrite the operator's registry into something
    other than what they wrote, it tells them the spelling is not the one the
    child reads. Duplicate detection stays case-insensitive either way.
    """

    if not isinstance(name, str) or not name or len(name) > MAX_ENVIRONMENT_NAME_CHARS:
        raise LauncherError("invalid_driver_environment")
    folded = name.upper()
    if folded not in ALLOWED_ENVIRONMENT_NAMES:
        raise LauncherError("unknown_driver_environment_name")
    if folded == CONFIG_ENVIRONMENT_VARIABLE and name != CONFIG_ENVIRONMENT_VARIABLE:
        # Spelled like the required variable, but not the name the child reads.
        raise LauncherError("unknown_driver_environment_name")
    return folded


def _require_matching_driver_config(
    environment: Mapping[str, str], alias: str, upstream: str
) -> None:
    """Read the driver's own document and refuse a registry that contradicts it."""

    path = _config_variable(environment)
    if not path:
        raise LauncherError("missing_driver_config_variable")
    config = _driver_config(path)
    if config.connection_alias != alias or config.upstream_workspace_uid != upstream:
        raise LauncherError("driver_config_mismatch")


def _config_variable(environment: Mapping[str, str]) -> str | None:
    """The configuration path, read exactly as the child will read it.

    Admission already refused every other spelling of this name, so this
    case-sensitive lookup is the same lookup ``driver_main._pinned_config``
    performs: what this launcher cross-checked is what that child will find.
    """

    return environment.get(CONFIG_ENVIRONMENT_VARIABLE)


def _driver_config(path: str) -> OperatorDriverConfig:
    try:
        return load_operator_driver_config(path)
    except DriverConfigError as error:
        # The loader's own code says which rule closed; this launcher reports
        # one closed code and leaves the detail to ``DRIVER.md``, so no path or
        # file content is ever printed here.
        raise LauncherError("invalid_driver_config") from error


def _admitted_path(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_CHARS:
        raise LauncherError(code)
    if any(ord(character) in _CONTROL for character in value):
        raise LauncherError(code)
    if not os.path.isabs(value):
        raise LauncherError(code)
    return value


# -- the data directory ---------------------------------------------------


def _admitted_data_dir(value: Any) -> Path:
    """An absolute directory that is empty or does not exist yet.

    The pilot creates its own store. Refusing a directory that already holds
    anything is what keeps this experiment from opening, upgrading or adopting
    a real Work Stack store; the operator picks a fresh path instead.
    """

    absolute = Path(_admitted_path(value, "invalid_data_dir"))
    if absolute.is_dir():
        if any(absolute.iterdir()):
            raise LauncherError("data_dir_not_empty")
        return absolute
    if absolute.exists():
        raise LauncherError("invalid_data_dir")
    return absolute


# -- the command line -----------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledge_driver_launcher",
        description=(
            "Serve one loopback Work Stack owner with the knowledge drivers an "
            "operator pinned. Source-checkout pilot; not an installed adapter."
        ),
    )
    parser.add_argument(
        "--drivers-config",
        required=True,
        help="absolute path of the workstack.knowledge-drivers.v1 file",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="absolute path of an empty or absent data directory for this pilot",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="loopback port from 0 to 65535; 0 asks the OS for a free one",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="admit the configuration and exit; reads no key, starts nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Admit the configuration, then either report it or serve it."""

    arguments = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        drivers = build_driver_registry(arguments.drivers_config)
        if arguments.check_config:
            print("configuration admitted: {} driver(s)".format(len(drivers)))
            return 0
        if arguments.data_dir is None:
            raise LauncherError("invalid_data_dir")
        if isinstance(arguments.port, bool) or not 0 <= arguments.port <= 65535:
            raise LauncherError("invalid_port")
        data_dir = _admitted_data_dir(arguments.data_dir)
    except LauncherError as refusal:
        print("configuration refused: {}".format(refusal.code), file=sys.stderr)
        return 2
    _serve(data_dir, int(arguments.port), drivers)
    return 0


def _serve(
    data_dir: Path, port: int, drivers: Mapping[str, KnowledgeDriverBinding]
) -> None:
    """Open the pilot store, publish the loopback URL, and serve until Ctrl-C.

    ``create_server`` is the released constructor and takes the lease, admits
    the registry again and binds the socket itself. The only thing printed is
    the URL: no path, no alias, no configuration and no key.
    """

    server = create_server(
        WorkStack(Store(data_dir)),
        LOOPBACK_HOST,
        port,
        knowledge_drivers=drivers,
    )
    try:
        print("http://{}:{}/".format(LOOPBACK_HOST, server.actual_port))
        sys.stdout.flush()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
