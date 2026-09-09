#!/usr/bin/env python3
"""Explicit local CLI for the bounded remote provision-install driver.

Default behaviour is inspect/plan only: it probes the named target read-only
and prints the plan.  Installing requires ``--apply`` on the command line,
and even then the driver re-probes the same target and re-plans immediately
before the install, so the plan printed here authorises nothing on its own.

Every part of the target is named explicitly on argv.  This CLI never reads
an SSH config, never enumerates hosts, never consults a saved profile or the
SSOT, and never picks a host for you.  It also never activates a connection:
a successful install leaves the active profile and any running server exactly
as they were, and the next step is the existing Test -> proof -> activate ->
confirm/restore protocol, driven separately.

Exit codes:

  0  plan produced, or install completed with an exact matching receipt
  2  invalid input, refused plan, or a clean remote refusal
  3  the install outcome is unknown and must be reconciled by hand
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

from remote_provision_driver import (  # noqa: E402
    INSTALL_TIMEOUT_SECONDS,
    OUTCOME_INSTALLED,
    OUTCOME_UNKNOWN,
    PROBE_TIMEOUT_SECONDS,
    ArtifactSelection,
    DriverError,
    DriverProfile,
    ProbeError,
    apply_remote_install,
    encode_document,
    inspect_remote_target,
    render_inspection,
    render_outcome,
    select_artifact,
)
from remote_provision_installer import MAX_ARCHIVE, MAX_SIDECAR  # noqa: E402
from remote_skill_install import SkillInstallError, run_skill_install  # noqa: E402


EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNKNOWN = 3


def build_parser() -> argparse.ArgumentParser:
    """Every target field is required and explicit; nothing is discovered.

    There is deliberately no ``--force``, ``--overwrite``, ``--replace``,
    ``--steal``, ``--delete``, or ``--retry`` option.  A refusal from the
    planner or the installer is the answer, not an obstacle.
    """

    parser = argparse.ArgumentParser(
        prog="remote_provision_install",
        description=(
            "Inspect a named remote Linux target and plan an install. "
            "Pass --apply to run the install the plan authorised."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--ssh-host-alias", required=True)
    parser.add_argument("--ssh-executable", required=True)
    parser.add_argument("--remote-python", required=True)
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--expected-workspace-uid", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument(
        "--expect-digest",
        required=True,
        help=(
            "sha256:<hex> of the archive the operator intends to install. "
            "The archive is refused unless it hashes to exactly this."
        ),
    )
    parser.add_argument("--probe-timeout", type=float, default=PROBE_TIMEOUT_SECONDS)
    parser.add_argument("--install-timeout", type=float, default=INSTALL_TIMEOUT_SECONDS)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run the install. Without this flag the CLI stops after the plan.",
    )
    return parser


def build_skill_parser() -> argparse.ArgumentParser:
    """Opt-in Skill install from a verified app. Separate from SSH provision.

    Does not change the frozen SSH provision-install argv. HOME must already
    be the POSIX user's home; this parser never discovers it.
    """

    parser = argparse.ArgumentParser(
        prog="remote_provision_install",
        description=(
            "Install the Work Stack agent Skill from a verified installed app "
            "into $HOME/.agents/skills/work-stack. Inspect/plan is the default. "
            "Pass --apply to write. Unpack and app provision never do this."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--install-skill", action="store_true", required=True)
    parser.add_argument("--install-root", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the Skill. Without this flag the CLI stops after the plan.",
    )
    return parser


def _read_bounded(path: str, limit: int, what: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            payload = handle.read(limit + 1)
    except OSError:
        raise DriverError("DRIVER_ARTIFACT_INVALID", f"{what} could not be read") from None
    if not payload or len(payload) > limit:
        raise DriverError("DRIVER_ARTIFACT_INVALID", f"{what} is empty or exceeds its bound")
    return payload


def load_artifact(archive_path: str, sidecar_path: str, expected_digest: str) -> ArtifactSelection:
    """Select the archive the operator named, and prove it is that one."""

    archive = _read_bounded(archive_path, MAX_ARCHIVE, "archive")
    sidecar = _read_bounded(sidecar_path, MAX_SIDECAR, "sidecar")
    artifact = select_artifact(archive, sidecar)
    if artifact.digest != expected_digest:
        raise DriverError(
            "DRIVER_ARTIFACT_INVALID", "archive digest does not match --expect-digest"
        )
    return artifact


def build_profile(arguments: argparse.Namespace) -> DriverProfile:
    return DriverProfile(
        ssh_host_alias=arguments.ssh_host_alias,
        remote_python=arguments.remote_python,
        remote_app_dir=arguments.install_root,
        remote_data_dir=arguments.data_root,
        expected_workspace_id=arguments.expected_workspace_uid,
    )


def _write(document: object) -> None:
    sys.stdout.buffer.write(encode_document(document))
    sys.stdout.buffer.flush()


def _write_error(code: str, detail: str) -> int:
    payload = json.dumps(
        {"code": code, "detail": detail},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii", "replace")
    sys.stderr.buffer.write(payload + b"\n")
    sys.stderr.buffer.flush()
    return EXIT_REFUSED


def run_skill_cli(argv: list[str]) -> int:
    arguments = build_skill_parser().parse_args(argv)
    home = os.environ.get("HOME")
    if not home:
        return _write_error("HOME_REQUIRED", "HOME is required for skill install")
    try:
        document = run_skill_install(
            install_root=arguments.install_root,
            home=home,
            apply=arguments.apply,
        )
    except SkillInstallError as error:
        return _write_error(error.code, error.detail or error.code)
    _write(document)
    if document.get("action") == "refuse" or document.get("outcome") == "refused":
        return EXIT_REFUSED
    return EXIT_OK


def run(argv: list[str]) -> int:
    if "--install-skill" in argv:
        return run_skill_cli(argv)
    arguments = build_parser().parse_args(argv)
    try:
        artifact = load_artifact(
            arguments.archive, arguments.sidecar, arguments.expect_digest
        )
        profile = build_profile(arguments)
        inspection = inspect_remote_target(
            profile,
            arguments.owner,
            artifact,
            ssh_executable=arguments.ssh_executable,
            timeout=arguments.probe_timeout,
        )
    except DriverError as error:
        return _write_error(error.code, error.detail)
    except ProbeError as error:
        return _write_error(error.code, error.detail)

    if not arguments.apply:
        _write(render_inspection(inspection))
        return EXIT_OK if inspection.install_allowed else EXIT_REFUSED

    try:
        outcome = apply_remote_install(
            inspection,
            artifact,
            apply=True,
            ssh_executable=arguments.ssh_executable,
            timeout=arguments.install_timeout,
            probe_timeout=arguments.probe_timeout,
        )
    except DriverError as error:
        return _write_error(error.code, error.detail)

    _write(render_outcome(inspection, outcome))
    if outcome.outcome == OUTCOME_INSTALLED:
        return EXIT_OK
    if outcome.outcome == OUTCOME_UNKNOWN:
        return EXIT_UNKNOWN
    return EXIT_REFUSED


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
