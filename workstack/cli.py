"""Command-line interface for the portable work-stack."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

from . import agent_apply_admission, agent_runtime
from . import checkpoint_state_cli, cli_capabilities, cli_output, cli_routing, cli_writer
from . import cli_writer_reports
from . import cli_storage_receipts as receipts
from .cli_parser_root import parser
from .knowledge_driver_registry import load_driver_registry
from .server import KnowledgeDriverBinding, serve
from .service import DomainError, WorkStack
from .maintenance import backup_store, initialize_store, relocate_store, restore_store, verify_backup
from .snapshot_export import write_snapshot_file
from .store import Store
from .storage.migration import (
    StorageMigrationError,
    execute_v3_migration,
    load_migration_receipt,
    plan_v3_migration,
    preview_v3_migration,
    resume_v3_migration,
    verify_v3_migration_artifacts,
)
from .storage.validation import validate_storage_path
from .storage.v4_backup import (
    V4BackupError,
    restore_v4_backup,
    verify_v4_backup,
    write_v4_backup,
)


PROJECT_DATA = Path(__file__).resolve().parents[1] / "data"
AGENT_APPLY_LIMIT = agent_apply_admission.AGENT_APPLY_LIMIT
_APPLY_COMMIT_UNKNOWN = (
    "agent apply commit is unknown; inspect the Task revision before retrying"
)


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _checkpoint_facts_api():
    from .checkpoint_facts import (
        project_checkpoint_facts,
        render_checkpoint_facts,
        validate_checkpoint_request,
    )
    return validate_checkpoint_request, project_checkpoint_facts, render_checkpoint_facts


def emit_checkpoint_facts(audit: object, arguments: argparse.Namespace) -> None:
    _validate, project, render = _checkpoint_facts_api()
    del _validate
    facts = project(audit, workspace_uid=arguments.workspace_uid, task_id=arguments.task)
    print(render(facts, format=arguments.format), end="")


def _checkpoint_facts_emitter(arguments: argparse.Namespace):
    return lambda audit: emit_checkpoint_facts(audit, arguments)


def forward_checkpoint_state(
    store: Store, raw: bytes, checkpoint_id: str, idempotency_key: str
) -> int:
    """One explicit invocation, one POST, to a running owner only.

    The owner metadata decides the route before any Store initialization: an
    absent or unusable advertisement refuses here rather than falling back to a
    local write, because this command has no local form.
    """

    body = checkpoint_state_cli.parse_body(raw)
    owner_state = cli_writer.owner_metadata_state(store)
    if owner_state == cli_writer.OWNER_ABSENT:
        raise OSError("Work Stack server is not running for this data directory")
    emit(
        checkpoint_state_cli.forward_checkpoint_state(
            store,
            owner_state,
            checkpoint_id,
            body,
            idempotency_key,
            coordinates_reader=_server_coordinates,
            request_json=_request_json,
        )
    )
    return 0


def forward_report_create(store: Store, date: str) -> int:
    """One explicit invocation creates one daily-v1 draft through a running owner.

    There is no exclusive-local form: absent or unusable owner metadata refuses
    here rather than initializing, repairing, or writing reports.json locally.
    """

    owner_state = cli_writer.owner_metadata_state(store)
    if owner_state == cli_writer.OWNER_ABSENT:
        raise OSError("Work Stack server is not running for this data directory")
    emit(
        cli_writer_reports.forward_report_create(
            store,
            owner_state,
            date,
            coordinates_reader=_server_coordinates,
            request_json=_request_json,
        )
    )
    return 0


def forward_capture(store: Store, raw: bytes, idempotency_key: str | None) -> int:
    if len(raw) > 64 * 1024:
        raise ValueError("capture packet exceeds 64 KiB")
    try:
        packet = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("stdin must contain one UTF-8 JSON object") from error
    if not isinstance(packet, dict):
        raise ValueError("stdin must contain one JSON object")
    canonical = json.dumps(
        packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    outgoing = canonical
    key = idempotency_key or ("cli:" + hashlib.sha256(canonical).hexdigest())
    if not store.server_info_path.is_file() or not store.capture_token_path.is_file():
        raise OSError("Work Stack server is not running for this data directory")
    try:
        info = json.loads(store.server_info_path.read_text(encoding="utf-8"))
        token = store.capture_token_path.read_text(encoding="utf-8").strip()
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError("Work Stack server runtime metadata is invalid") from error
    if (
        not isinstance(info, dict)
        or info.get("version") != 1
        or info.get("host") not in ("127.0.0.1", "::1", "localhost")
        or not isinstance(info.get("port"), int)
        or not 1 <= info["port"] <= 65535
        or not token
    ):
        raise OSError("Work Stack server runtime metadata is invalid")
    connection = http.client.HTTPConnection(info["host"], info["port"], timeout=10)
    try:
        connection.request(
            "POST",
            "/api/v1/captures",
            body=outgoing,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Idempotency-Key": key,
            },
        )
        response = connection.getresponse()
        status = response.status
        result = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
    emit(result)
    return 0 if 200 <= status < 300 else 2


def _server_coordinates(store: Store) -> tuple[str, int] | None:
    if not store.server_info_path.is_file():
        return None
    try:
        info = json.loads(store.server_info_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError("Work Stack server runtime metadata is invalid") from error
    if (
        not isinstance(info, dict)
        or info.get("version") != 1
        or info.get("host") not in ("127.0.0.1", "::1", "localhost")
        or not isinstance(info.get("port"), int)
        or isinstance(info.get("port"), bool)
        or not 1 <= info["port"] <= 65535
    ):
        raise OSError("Work Stack server runtime metadata is invalid")
    return info["host"], info["port"]


def _request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    outgoing = None
    request_headers = dict(headers or {})
    if body is not None:
        outgoing = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request(method, path, body=outgoing, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OSError("Work Stack server returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise OSError("Work Stack server returned an invalid response")
        return response.status, payload
    finally:
        connection.close()


def _task_from_detail(payload: dict[str, object]) -> dict[str, object] | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    task = data.get("task", data)
    return task if isinstance(task, dict) else None


def _matches_agent_result(
    task: dict[str, object],
    expected_revision: int,
    changes: dict[str, object],
    task_id: str,
) -> bool:
    revision = task.get("revision")
    if type(revision) is not int or task.get("id") != task_id:
        return False
    return revision == expected_revision + 1 and all(
        task.get(field) == value for field, value in changes.items()
    )


def _forward_agent_apply(
    store: Store,
    packet: dict[str, object],
    intent_id: str,
    coordinates: tuple[str, int],
) -> int:
    host, port = coordinates
    status, session = _request_json(host, port, "GET", "/api/v1/session")
    session_data = session.get("data")
    if (
        status != 200
        or not isinstance(session_data, dict)
        or not isinstance(session_data.get("csrf_token"), str)
    ):
        raise OSError("Work Stack server session could not be established")
    status, storage = _request_json(host, port, "GET", "/api/v1/storage")
    storage_data = storage.get("data")
    if status != 200 or not isinstance(storage_data, dict):
        raise OSError("Work Stack storage identity could not be verified")
    if storage_data.get("workspace_id") != packet["workspace_id"]:
        raise ValueError("agent apply workspace_id does not match the running server")
    origin_host = "[{}]".format(host) if ":" in host else host
    headers = {
        "Origin": "http://{}:{}".format(origin_host, port),
        "X-WorkStack-CSRF": session_data["csrf_token"],
        "X-WorkStack-Agent-Intent": intent_id,
    }
    task_id = str(packet["task_id"])
    expected_revision = int(packet["expected_revision"])
    changes = dict(packet["changes"])
    request_body = {**changes, "revision": expected_revision}
    path = "/api/v1/tasks/{}".format(quote(task_id, safe=""))
    try:
        status, result = _request_json(
            host, port, "PATCH", path, body=request_body, headers=headers
        )
    except (OSError, TimeoutError):
        try:
            verify_status, verified = _request_json(host, port, "GET", path)
        except (OSError, TimeoutError):
            raise OSError(_APPLY_COMMIT_UNKNOWN) from None
        task = _task_from_detail(verified) if verify_status == 200 else None
        if task is not None and _matches_agent_result(
            task, expected_revision, changes, task_id
        ):
            emit({
                "data": task,
                "meta": {
                    "intent_id": intent_id,
                    "mode": "running-server",
                    "verified_after_transport_loss": True,
                },
            })
            return 0
        raise OSError(_APPLY_COMMIT_UNKNOWN)
    if 200 <= status < 300:
        task = _task_from_detail(result)
        if task is None:
            raise OSError("Work Stack server returned an invalid Task response")
        emit({
            "data": task,
            "meta": {
                "intent_id": intent_id,
                "mode": "running-server",
                "verified_after_transport_loss": False,
            },
        })
        return 0
    emit(result)
    return 2


def apply_agent_update(
    store: Store,
    packet: dict[str, object],
    intent_id: str,
    *,
    route: str | None = None,
) -> int:
    agent_apply_admission.require_intent_id(intent_id)
    coordinates = None if route == "exclusive-local-store" else _server_coordinates(store)
    if route == "running-server" and coordinates is None:
        raise OSError("Work Stack server is not running for this data directory")
    if coordinates is not None:
        return _forward_agent_apply(store, packet, intent_id, coordinates)
    agent_apply_admission.require_held_local_apply(store, packet["workspace_id"])
    with store.transaction():
        agent_apply_admission.require_held_local_apply(store, packet["workspace_id"])
        stack = WorkStack(store)
        task = stack.patch_task(
            str(packet["task_id"]),
            {**dict(packet["changes"]), "revision": packet["expected_revision"]},
        )
    emit({
        "data": task,
        "meta": {
            "intent_id": intent_id,
            "mode": "exclusive-local-store",
            "verified_after_transport_loss": False,
        },
    })
    return 0


def _run_maintenance(arguments: argparse.Namespace, store: Store) -> None:
    if arguments.action == "backup":
        emit(receipts.backup_receipt(backup_store(store.root, arguments.out)))
        return
    if arguments.action == "verify":
        emit(receipts.backup_receipt(verify_backup(arguments.archive)))
        return
    if arguments.action == "restore":
        emit(receipts.restore_receipt(restore_store(
            arguments.archive,
            arguments.to,
            replace=arguments.replace,
            safety_backup_dir=arguments.safety_backups,
        )))
        return
    if arguments.action == "initialize":
        emit(receipts.initialize_receipt(initialize_store(store.root)))
        return
    emit(receipts.relocate_receipt(relocate_store(store.root, arguments.to)))


def _run_storage_migration(arguments: argparse.Namespace) -> int:
    action = arguments.migration_action
    if action == "plan":
        emit(receipts.migration_plan_receipt(plan_v3_migration(
            arguments.source,
            candidate_override=arguments.candidate,
            backup_override=arguments.backup,
        )))
        return 0
    if action == "preview":
        emit(receipts.migration_preview_receipt(preview_v3_migration(
            arguments.source,
            candidate_created_at=arguments.candidate_created_at,
            candidate_override=arguments.candidate,
            backup_override=arguments.backup,
        )))
        return 0
    if action == "execute":
        emit(receipts.migration_execute_receipt(execute_v3_migration(
            arguments.source,
            candidate_created_at=arguments.candidate_created_at,
            candidate_override=arguments.candidate,
            backup_override=arguments.backup,
            expected_source_digest=arguments.expected_source_digest,
            expected_conversion_digest=arguments.expected_conversion_digest,
        )))
        return 0
    if action == "resume":
        emit(receipts.migration_resume_receipt(resume_v3_migration(
            arguments.source,
            candidate_created_at=arguments.candidate_created_at,
            candidate_path=arguments.candidate,
            backup_path=arguments.backup,
            expected_source_digest=arguments.expected_source_digest,
            expected_conversion_digest=arguments.expected_conversion_digest,
        )))
        return 0
    if action == "verify":
        emit(receipts.migration_verify_receipt(verify_v3_migration_artifacts(
            arguments.source,
            candidate_root=arguments.candidate,
            backup_path=arguments.backup,
            receipt_path=arguments.receipt,
        )))
        return 0
    emit(load_migration_receipt(arguments.path))
    return 0


def _run_v4_backup(arguments: argparse.Namespace) -> int:
    if arguments.v4_backup_action == "create":
        artifact = write_v4_backup(arguments.source, arguments.out)
        emit(receipts.v4_backup_receipt("backed_up", artifact))
        return 0
    if arguments.v4_backup_action == "verify":
        artifact = verify_v4_backup(arguments.archive)
        emit(receipts.v4_backup_receipt("verified", artifact))
        return 0
    emit(receipts.v4_restore_receipt(restore_v4_backup(arguments.archive, arguments.to)))
    return 0


def _run_storage(arguments: argparse.Namespace) -> int:
    if arguments.action == "v4-backup":
        try:
            return _run_v4_backup(arguments)
        except (V4BackupError, ValueError, OSError) as error:
            emit(receipts.refusal_receipt(error, "V4_BACKUP_REFUSED", activated=False))
            return 2
    if arguments.action == "migration":
        try:
            return _run_storage_migration(arguments)
        except (StorageMigrationError, ValueError, OSError) as error:
            emit(receipts.refusal_receipt(error, "STORAGE_MIGRATION_REFUSED"))
            return 2
    report = validate_storage_path(arguments.path)
    emit(receipts.validation_receipt(report))
    return 0 if report.valid else 2


def _run_backlog(arguments: argparse.Namespace, stack: WorkStack) -> None:
    if arguments.action == "add":
        result = stack.add_task(
            arguments.title,
            arguments.detail,
            arguments.priority,
            arguments.due,
            arguments.tag,
            arguments.objective,
            arguments.parent,
            arguments.depends_on,
        )
    elif arguments.action == "list":
        result = stack.list_tasks(arguments.status)
    elif arguments.action == "show":
        result = stack.get_task(arguments.id)
    elif arguments.action == "note":
        result = stack.add_task_note(arguments.id, arguments.text)
    elif arguments.action == "subtask":
        if arguments.operation == "add":
            result = stack.add_subtask(
                arguments.task,
                arguments.subtask_or_title,
                arguments.priority,
            )
        else:
            result = stack.set_subtask_status(
                arguments.task,
                arguments.subtask_or_title,
                cli_routing.planning_status(arguments.operation),
            )
    else:
        result = stack.set_task_status(
            arguments.id, cli_routing.planning_status(arguments.action)
        )
    emit(result)


def _run_okr(arguments: argparse.Namespace, stack: WorkStack) -> None:
    if arguments.action == "add-objective":
        result = stack.add_objective(arguments.text, arguments.quarter)
    elif arguments.action == "add-key-result":
        result = stack.add_key_result(arguments.objective, arguments.text, arguments.target)
    elif arguments.action == "list":
        result = stack.list_objectives(arguments.status)
    elif arguments.action == "link":
        result = stack.link_task(arguments.objective, arguments.task)
    elif arguments.action == "progress":
        result = stack.set_key_result_progress(
            arguments.objective,
            arguments.key_result,
            arguments.value,
        )
    else:
        result = stack.objective_rollup()
    emit(result)


def _run_worklog(arguments: argparse.Namespace, stack: WorkStack) -> None:
    if arguments.action == "checkin":
        result = stack.checkin(arguments.time, arguments.date)
    elif arguments.action == "add":
        result = stack.add_worklog(
            arguments.task,
            arguments.done,
            arguments.next_items,
            arguments.blocker,
            arguments.date,
        )
    elif arguments.action == "latest-checkpoint":
        emit_checkpoint_facts(stack.list_checkpoint_audit(), arguments)
        return
    else:
        result = stack.list_worklog(arguments.date)
    emit(result)


def _run_weekly(arguments: argparse.Namespace, stack: WorkStack) -> None:
    emit(stack.weekly_report(arguments.end, arguments.days))


def _run_note(arguments: argparse.Namespace, stack: WorkStack) -> None:
    emit(stack.add_note(arguments.text, arguments.link))


def _run_snapshot(arguments: argparse.Namespace, stack: WorkStack) -> None:
    if arguments.action == "preview":
        artifact = stack.planning_snapshot(arguments.task)
        emit({
            "snapshot": artifact.snapshot,
            "digest": artifact.digest,
            "filename": artifact.filename,
            "omissions": list(artifact.omissions),
        })
        return
    artifact = stack.confirmed_snapshot_export(
        arguments.task,
        arguments.expected_revision,
        arguments.expected_digest,
        arguments.confirm_disclosure,
    )
    output = write_snapshot_file(arguments.out, artifact.canonical_bytes)
    emit({
        "path": str(output),
        "digest": artifact.digest,
        "revision": artifact.snapshot["revision"],
    })


def _run_graph(
    arguments: argparse.Namespace,
    stack: WorkStack,
    *,
    knowledge_drivers: Mapping[str, KnowledgeDriverBinding] | None = None,
) -> None:
    """Export one graph, or serve one owner with the pinned drivers it was given.

    ``knowledge_drivers`` is ``None`` for every caller that did not name a
    registry file, and that call is the pre-existing one, argument for
    argument.
    """

    if arguments.action == "export":
        output = Path(arguments.out).resolve()
        output.write_text(
            json.dumps(stack.snapshot(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(output)
        return
    if arguments.seed_demo:
        stack.store.seed_demo(PROJECT_DATA)
    if getattr(arguments, "exit_with_parent", False):
        _bind_linux_process_lifetime_to_parent()
    public_port = getattr(arguments, "public_port", None)
    if knowledge_drivers is not None:
        serve(
            stack,
            arguments.host,
            arguments.port,
            public_port=public_port,
            knowledge_drivers=knowledge_drivers,
        )
        return
    if public_port is None:
        serve(stack, arguments.host, arguments.port)
        return
    serve(stack, arguments.host, arguments.port, public_port=public_port)


def _bind_linux_process_lifetime_to_parent() -> None:
    """Fail closed with the owning SSH session instead of becoming an orphan."""

    if not sys.platform.startswith("linux"):
        raise OSError("--exit-with-parent requires Linux")
    import ctypes
    import signal

    parent_pid = os.getppid()
    if parent_pid <= 1:
        raise OSError("--exit-with-parent requires a live parent process")

    def terminate_with_parent(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate_with_parent)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error_number = ctypes.get_errno()
        raise OSError(error_number, "could not bind the server lifetime to its parent")
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


STACK_COMMANDS = {
    "backlog": _run_backlog,
    "okr": _run_okr,
    "worklog": _run_worklog,
    "weekly": _run_weekly,
    "note": _run_note,
    "snapshot": _run_snapshot,
    "graph": _run_graph,
}


def _store_from_args(arguments: argparse.Namespace) -> Store:
    return Store(arguments.data_dir) if arguments.data_dir else Store()


def _execute_stack(arguments: argparse.Namespace, store: Store) -> int:
    stack = WorkStack(store, initialize=arguments.domain != "snapshot")
    STACK_COMMANDS[arguments.domain](arguments, stack)
    return 0


def _owner_forwarded_write(arguments: argparse.Namespace):
    return cli_routing.owner_writer(
        arguments,
        coordinates_reader=_server_coordinates,
        request_json=_request_json,
    )


def _owner_forwarded_read(arguments: argparse.Namespace):
    return cli_routing.owner_reader(
        arguments,
        coordinates_reader=_server_coordinates,
        request_json=_request_json,
    )


def _dispatch_path_tool(arguments: argparse.Namespace) -> int:
    if arguments.domain == "storage":
        return _run_storage(arguments)
    _run_maintenance(arguments, _store_from_args(arguments))
    return 0


def _dispatch_agent_envelope(arguments: argparse.Namespace) -> int:
    if arguments.action == "checkpoint":
        arguments.checkpoint_raw = sys.stdin.buffer.read(AGENT_APPLY_LIMIT + 1)
    return agent_runtime.run_agent_command(
        args=arguments,
        stdout=sys.stdout,
        stderr=sys.stderr,
        dependencies=agent_runtime._default_runtime_dependencies(),
    )


def _dispatch_process_owner(arguments: argparse.Namespace) -> int:
    """Admit the operator's driver registry before this process takes a store.

    The registry is read here rather than in ``_run_graph`` because the Store,
    its lease, the socket and ``--seed-demo`` all happen after this line: a
    malformed configuration refuses the start, not a later request.

    With no registry the dispatch is the pre-existing one: the handler that
    ``STACK_COMMANDS["graph"]`` currently names, called with exactly its two
    arguments, so replacing that mapping entry still redirects an ordinary
    ``graph serve``.
    """

    drivers = load_driver_registry(
        getattr(arguments, "knowledge_drivers_config", None)
    )
    stack = WorkStack(_store_from_args(arguments), initialize=True)
    if drivers is None:
        STACK_COMMANDS["graph"](arguments, stack)
        return 0
    _run_graph(arguments, stack, knowledge_drivers=drivers)
    return 0


def _dispatch_owner_required(arguments: argparse.Namespace) -> int:
    store = _store_from_args(arguments)
    if arguments.domain == "worklog":
        return forward_checkpoint_state(
            store,
            sys.stdin.buffer.read(checkpoint_state_cli.STDIN_LIMIT + 1),
            arguments.checkpoint,
            arguments.idempotency_key,
        )
    if arguments.domain == "report":
        return forward_report_create(store, arguments.date)
    return forward_capture(
        store,
        sys.stdin.buffer.read(64 * 1024 + 1),
        arguments.idempotency_key,
    )


def _dispatch_admitted(
    arguments: argparse.Namespace, capability: cli_capabilities.CliCapability
) -> int:
    emitter = emit
    if capability.command_key == cli_capabilities.LATEST_CHECKPOINT_KEY:
        validate, _project, _render = _checkpoint_facts_api()
        validate(workspace_uid=arguments.workspace_uid, task_id=arguments.task)
        emitter = _checkpoint_facts_emitter(arguments)
    return cli_routing.dispatch_ordinary(
        arguments,
        capability,
        run_local=_execute_stack,
        owner_writer_for=_owner_forwarded_write,
        owner_reader_for=_owner_forwarded_read,
        emit=emitter,
    )


def _dispatch_parsed(
    arguments: argparse.Namespace, capability: cli_capabilities.CliCapability
) -> int:
    family = cli_capabilities.command_family(capability)
    if family == cli_capabilities.FAMILY_PATH_TOOL:
        return _dispatch_path_tool(arguments)
    if family == cli_capabilities.FAMILY_AGENT_ENVELOPE:
        return _dispatch_agent_envelope(arguments)
    if family == cli_capabilities.FAMILY_PROCESS_OWNER:
        return _dispatch_process_owner(arguments)
    if family == cli_capabilities.FAMILY_OWNER_REQUIRED:
        return _dispatch_owner_required(arguments)
    if family == cli_capabilities.FAMILY_AGENT_APPLY:
        return agent_apply_admission.dispatch_admitted_apply(
            arguments,
            sys.stdin.buffer.read(AGENT_APPLY_LIMIT + 1),
            arguments.intent_id,
            apply=apply_agent_update,
        )
    if family == cli_capabilities.FAMILY_ADMITTED:
        return _dispatch_admitted(arguments, capability)
    raise OSError("unsupported Work Stack command")


def main(argv: list[str] | None = None) -> int:
    """Run one command line with UTF-8 stdout/stderr on every console code page.

    The encoding boundary wraps argument parsing too, so argparse's own usage
    and refusal messages survive a non-ASCII argument on a cp949 console
    instead of failing inside its own error path.
    """

    with cli_output.utf8_streams():
        return _run(argv)


def _run(argv: list[str] | None) -> int:
    arguments = parser().parse_args(argv)
    try:
        capability = cli_capabilities.require_capability(
            cli_capabilities.command_key_from_parsed(arguments)
        )
        return _dispatch_parsed(arguments, capability)
    except cli_capabilities.CapabilityRegistryError as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2
    except DomainError as error:
        print("error: {}: {}".format(error.code, error), file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2
