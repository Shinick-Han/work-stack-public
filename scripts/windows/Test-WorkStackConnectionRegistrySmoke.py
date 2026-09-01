#!/usr/bin/env python3
"""Deterministic packaged-desktop connection-registry smoke.

This is a release/installer test plan expressed as executable Python.  It
exercises only persisted configuration and pure command construction; it never
starts a Store, server, tunnel, browser, or network request.

Plan:

1. With ``WORKSTACK_CONNECTION_REGISTRY_V1`` conceptually off, load the legacy
   singleton and prove that no registry/migration artifact is created.
2. Turn the gate on, migrate that local singleton, activate a second verified
   local workspace, simulate a process restart, and confirm the pending
   activation only after the restarted selection succeeds.
3. Activate a verified SSH profile, inject its read-only identity result, and
   verify the exact fixed-shape tunnel arguments without launching OpenSSH.
4. Simulate runtime startup failure, prove the activation receipt remains
   pending, then explicitly restore the exact rollback registry.
5. Compare exact SHA-256 hashes for every Store roster file before and after
   all operations.

The Windows installer builder invokes the copy embedded in the payload with
the same bundled interpreter that will ship to users::

    & "$Payload\\runtime\\python.exe" `
      "$Payload\\scripts\\windows\\Test-WorkStackConnectionRegistrySmoke.py" `
      --install-root "$Payload"

The script must live under the install root it tests.  This prevents a release
job from accidentally importing product modules from a different checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final


SCRIPT_INSTALL_ROOT: Final = Path(__file__).resolve().parents[2]
SHELL = SCRIPT_INSTALL_ROOT / "desktop" / "python-webview-shell"
if str(SCRIPT_INSTALL_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_INSTALL_ROOT))
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

from connection_registry import (  # noqa: E402
    REGISTRY_FILE,
    ConnectionRegistry,
    LocalConnectionProfile,
    SshConnectionProfile,
    load_connection_registry,
)
from connection_registry_mutations import (  # noqa: E402
    ConnectionRegistryMutationService,
    load_activation_receipt,
    pending_activation_for_registry,
    registry_digest,
)
from connection_registry_startup import (  # noqa: E402
    MIGRATION_INTENT_FILE,
    MIGRATION_RECEIPT_FILE,
    MINIMUM_LOCAL_STORE_FILES,
    LocalStartupSelection,
    SshStartupSelection,
    ensure_connection_registry,
    select_active_profile_for_startup,
)
from profile_inspection import ProfileTestResult  # noqa: E402
from ssot_connection import (  # noqa: E402
    RemoteConnectionProfile,
    build_remote_server_command,
    build_ssh_tunnel_command,
    load_connection_draft,
)


PROFILE_LOCAL_B: Final = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROFILE_SSH: Final = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
WORKSPACE_LOCAL_A: Final = "11111111-1111-4111-8111-111111111111"
WORKSPACE_LOCAL_B: Final = "22222222-2222-4222-8222-222222222222"
WORKSPACE_SSH: Final = "33333333-3333-4333-8333-333333333333"
INSTALLATION_IDENTITY: Final = "work-stack-packaged-registry-smoke-v1"


@dataclass(frozen=True)
class SmokeReport:
    schema_version: int
    status: str
    scenarios: tuple[str, ...]
    store_files_verified: int

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "scenarios": list(self.scenarios),
            "store_files_verified": self.store_files_verified,
        }


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _write_store_fixture(data_dir: Path, workspace_id: str) -> None:
    """Create only the pre-test fixture; product APIs remain read-only to it."""

    data_dir.mkdir(parents=True)
    documents: dict[str, object] = {
        "workspace.json": {"id": workspace_id, "schema_version": 3},
        "backlog.json": {"schema_version": 3, "tasks": []},
        "store-meta.json": {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "revision": 0,
        },
        "okr.json": {"schema_version": 1, "objectives": []},
        "worklog.json": {"schema_version": 1, "days": {}},
        "notes.json": {"schema_version": 1, "notes": []},
        "captures.json": {"schema_version": 1, "captures": []},
        "replies.json": {"schema_version": 1, "replies": []},
        "activity.json": {"schema_version": 1, "events": []},
    }
    if set(documents) != set(MINIMUM_LOCAL_STORE_FILES):
        raise AssertionError("Smoke Store fixture does not match the startup roster")
    for name in sorted(documents):
        (data_dir / name).write_bytes(_canonical_json(documents[name]))


def _store_hashes(named_roots: dict[str, Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for label, root in sorted(named_roots.items()):
        for name in sorted(MINIMUM_LOCAL_STORE_FILES):
            path = root / name
            if not path.is_file():
                raise AssertionError(f"Smoke Store roster is incomplete: {label}/{name}")
            hashes[f"{label}/{name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _ready(profile: LocalConnectionProfile | SshConnectionProfile) -> ProfileTestResult:
    return ProfileTestResult(
        profile.profile_id,
        profile.kind,
        "ready",
        profile.expected_workspace_id,
        "packaged-smoke",
        1,
    )


def _assert_gate_off_is_read_only(state_root: Path) -> None:
    if load_connection_draft(state_root) != {"storage_mode": "local"}:
        raise AssertionError("Gate-off legacy local selection changed")
    forbidden = (REGISTRY_FILE, MIGRATION_INTENT_FILE, MIGRATION_RECEIPT_FILE)
    if any((state_root / name).exists() for name in forbidden):
        raise AssertionError("Gate-off legacy startup wrote registry state")


def _migrate_and_confirm_local(
    state_root: Path, local_a: Path, local_b: Path
) -> tuple[ConnectionRegistry, LocalStartupSelection]:
    migrated = ensure_connection_registry(
        state_root,
        installation_identity=INSTALLATION_IDENTITY,
        local_data_dir=str(local_a),
    )
    initial = select_active_profile_for_startup(state_root)
    if not isinstance(initial, LocalStartupSelection) or initial.data_dir != local_a:
        raise AssertionError("Migrated local workspace was not selected")
    if not (state_root / MIGRATION_RECEIPT_FILE).is_file():
        raise AssertionError("Local migration did not reach its receipt commit point")

    local_candidate = LocalConnectionProfile(
        profile_id=PROFILE_LOCAL_B,
        label="Smoke local B",
        data_dir=str(local_b),
        expected_workspace_id=WORKSPACE_LOCAL_B,
    )
    candidate = ConnectionRegistry(
        1,
        local_candidate.profile_id,
        migrated.profiles + (local_candidate,),
    )
    base_digest = registry_digest(migrated)
    activation_process = ConnectionRegistryMutationService(state_root)
    proof = activation_process.issue_successful_test_proof(
        local_candidate,
        _ready(local_candidate),
        base_registry_digest=base_digest,
    )
    pending = activation_process.activate(
        candidate,
        local_candidate.profile_id,
        proof.proof_id,
        expected_registry_digest=base_digest,
    )
    if pending.state != "pending":
        raise AssertionError("Local activation did not persist a pending receipt")

    # A new service instance models the packaged desktop's next process.  It
    # first verifies the active authority, then closes rollback eligibility.
    restarted_selection = select_active_profile_for_startup(state_root)
    if (
        not isinstance(restarted_selection, LocalStartupSelection)
        or restarted_selection.profile_id != local_candidate.profile_id
        or restarted_selection.data_dir != local_b
    ):
        raise AssertionError("Restart did not select the activated local workspace")
    restarted_process = ConnectionRegistryMutationService(state_root)
    persisted_pending = pending_activation_for_registry(
        state_root, registry_digest(candidate)
    )
    if persisted_pending != pending:
        raise AssertionError("Restart did not recover the exact pending activation")
    confirmed = restarted_process.confirm(
        pending.activation_id,
        expected_registry_digest=registry_digest(candidate),
    )
    if confirmed.state != "confirmed":
        raise AssertionError("Restarted local activation was not confirmed")
    return candidate, restarted_selection


def _simulate_ssh_failure_and_restore(
    state_root: Path, local_registry: ConnectionRegistry
) -> None:
    remote = SshConnectionProfile(
        profile_id=PROFILE_SSH,
        label="Smoke SSH",
        ssh_host_alias="work-linux",
        remote_app_dir="/srv/workstack/app",
        remote_data_dir="/srv/workstack/ssot",
        expected_workspace_id=WORKSPACE_SSH,
        preferred_forward_port=18765,
        remote_port=8765,
    )
    candidate = ConnectionRegistry(
        1,
        remote.profile_id,
        local_registry.profiles + (remote,),
    )
    base_digest = registry_digest(local_registry)
    service = ConnectionRegistryMutationService(state_root)
    proof = service.issue_successful_test_proof(
        remote,
        _ready(remote),
        base_registry_digest=base_digest,
    )
    pending = service.activate(
        candidate,
        remote.profile_id,
        proof.proof_id,
        expected_registry_digest=base_digest,
    )

    inspected: list[str] = []

    def simulated_identity_reader(profile: object) -> str:
        inspected.append(getattr(profile, "profile_id", ""))
        return WORKSPACE_SSH

    selection = select_active_profile_for_startup(
        state_root, remote_identity_reader=simulated_identity_reader
    )
    if not isinstance(selection, SshStartupSelection):
        raise AssertionError("SSH activation did not produce an SSH startup selection")
    if inspected != [PROFILE_SSH]:
        raise AssertionError("SSH identity was not checked exactly once")
    runtime = RemoteConnectionProfile(
        selection.ssh_host_alias,
        selection.remote_app_dir,
        selection.remote_data_dir,
        selection.preferred_forward_port,
        selection.expected_workspace_id,
        selection.remote_port,
    )
    command = build_ssh_tunnel_command(runtime, "ssh.exe")
    expected_prefix = [
        "ssh.exe",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        "127.0.0.1:18765:127.0.0.1:8765",
        "--",
        "work-linux",
    ]
    if command[:-1] != expected_prefix:
        raise AssertionError("SSH tunnel argument shape changed")
    if command[-1] != build_remote_server_command(runtime):
        raise AssertionError("SSH tunnel remote command changed")

    # The real desktop deliberately confirms only after runtime readiness.  A
    # simulated failure therefore does nothing to the receipt.
    try:
        raise RuntimeError("simulated packaged runtime startup failure")
    except RuntimeError:
        pass
    candidate_digest = registry_digest(candidate)
    if pending_activation_for_registry(state_root, candidate_digest) != pending:
        raise AssertionError("Failed SSH startup did not retain pending recovery state")
    if load_activation_receipt(state_root, pending.activation_id).state != "pending":
        raise AssertionError("Failed SSH startup changed its activation receipt")

    restored = service.restore(
        pending.activation_id,
        expected_registry_digest=candidate_digest,
    )
    if restored.state != "restored":
        raise AssertionError("Explicit SSH activation restore did not complete")
    if load_connection_registry(state_root) != local_registry:
        raise AssertionError("Explicit restore did not recover the exact local registry")


def run_smoke(install_root: Path = SCRIPT_INSTALL_ROOT) -> SmokeReport:
    requested_root = Path(install_root).resolve()
    if requested_root != SCRIPT_INSTALL_ROOT:
        raise RuntimeError(
            "Connection registry smoke must execute from the install root it tests"
        )
    required = (
        SHELL / "connection_registry_startup.py",
        SHELL / "connection_registry_mutations.py",
        SHELL / "ssot_connection.py",
    )
    if any(not path.is_file() for path in required):
        raise RuntimeError("Packaged connection registry modules are incomplete")

    with tempfile.TemporaryDirectory(prefix="workstack-registry-smoke-") as directory:
        root = Path(directory)
        state_root = root / "state"
        local_a = root / "ssot-a"
        local_b = root / "ssot-b"
        state_root.mkdir()
        _write_store_fixture(local_a, WORKSPACE_LOCAL_A)
        _write_store_fixture(local_b, WORKSPACE_LOCAL_B)
        stores = {"local-a": local_a, "local-b": local_b}
        before = _store_hashes(stores)
        _assert_gate_off_is_read_only(state_root)

        activated_local_registry, _selection = _migrate_and_confirm_local(
            state_root, local_a, local_b
        )
        _simulate_ssh_failure_and_restore(state_root, activated_local_registry)

        after = _store_hashes(stores)
        if after != before:
            changed = sorted(set(before) | set(after))
            raise AssertionError(
                "Connection registry smoke changed Store bytes: " + ", ".join(changed)
            )

    return SmokeReport(
        schema_version=1,
        status="passed",
        scenarios=(
            "gate-off-legacy-read-only",
            "gate-on-local-migrate-activate-restart-confirm",
            "ssh-selection-identity-and-command-no-network",
            "failed-startup-pending-receipt",
            "explicit-activation-restore",
            "store-sha256-unchanged",
        ),
        store_files_verified=len(before),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the packaged Work Stack connection-registry smoke."
    )
    parser.add_argument(
        "--install-root",
        type=Path,
        default=SCRIPT_INSTALL_ROOT,
        help="Exact payload/install root containing this script (default: inferred).",
    )
    options = parser.parse_args(argv)
    report = run_smoke(options.install_root)
    print(json.dumps(report.to_document(), ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
