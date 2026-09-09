#!/usr/bin/env python3
"""Create a disposable synthetic Work Stack dogfood fixture via product APIs.

Both formats are historical: ``--format v3`` writes the nine-document
schema-3 authority the released migration and validation seams accept, and
``--format v4`` converts one of those v3 sources. The dataset itself is
always produced by the released WorkStack APIs, so semantics, deterministic
identifiers and frozen timestamps come from the product rather than from
hand-written documents.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterator
from unittest import mock

CHECKOUT_ROOT = Path(__file__).resolve(strict=True).parents[1]
sys.path.insert(0, str(CHECKOUT_ROOT))

from workstack.context_projection import project_context_items
from workstack.service import WorkStack
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.migration_conversion import convert_v3_documents
# The frozen nine-document roster the v3 migration source seam itself accepts.
# Taking the names from there rather than from this build's DEFAULTS is what
# keeps "historical v3" a fixed fact instead of whatever the current schema is.
from workstack.storage.migration_source import V3_SOURCE_FILES
# The document serializer the released store writes its own JSON with. The v3
# fixture has to be byte-shaped like a store wrote it, and re-spelling the call
# here would let the two drift apart silently.
from workstack.store import Store, _serialized_json_bytes

NOW = "2026-09-05T12:00:00Z"
TODAY = "2026-09-05"
QUARTER = "2026-Q3"
UUID_NAMESPACE = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
RUNTIME_DIRNAME = ".dogfood-runtime"

OBJECTIVE_COMPACT = "Dogfood compact outcomes"
OBJECTIVE_REVIEW = "Dogfood review and context"
KR_LINKED = "Linked KR with explicit Tasks"
KR_ZERO = "Zero-linked KR with no Task refs"
KR_REVIEW = "Observable review checkpoint"
TASK_DONE = "Done prerequisite"
TASK_BLOCKED = "Blocked successor"
TASK_FREE = "Independent selected task"
NOTE_TEXT = "Shared dogfood context"
CHECKPOINT_DONE = "Recorded the disposable fixture checkpoint"
CHECKPOINT_NEXT = "Human smoke on source launch"

FORMAT_MEANINGS = {
    "v3": (
        "Released v3 document composition admits optional scoped Task "
        "key_result_refs."
    ),
    "v4": (
        "Experimental v4 conversion has no key_result_refs field and refuses "
        "that patch; do not migrate a v3-with-refs store."
    ),
}


class FixtureRefused(ValueError):
    """Content-free refusal to write a fixture into an unsafe destination."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def live_ssot_paths() -> tuple[Path, ...]:
    """Known live SSOT locations this script must never create or mutate."""

    paths = [Path.home() / "WorkStack" / "SSOT" / "main"]
    configured = os.environ.get("WORK_STACK_HOME")
    if configured:
        configured_path = Path(configured).expanduser()
        paths.extend((configured_path, configured_path.resolve()))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        paths.append(Path(local_app_data) / "WorkStack" / "data")
    paths.append(Path.home() / ".local" / "share" / "workstack")
    return tuple(paths)


def is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError as error:
        raise FixtureRefused("destination_unreadable") from error
    attributes = getattr(details, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse)


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def is_live_ssot(path: Path) -> bool:
    candidate = _normalized(path)
    for live in live_ssot_paths():
        live_normalized = _normalized(live)
        if candidate == live_normalized:
            return True
        if candidate.startswith(live_normalized + os.sep):
            return True
    return False


def refuse_alias_chain(lexical: Path) -> None:
    """Refuse a link/junction/reparse point on the path or any existing ancestor.

    The walk is lexical and runs before any resolve, Store, or mkdir, so an
    aliased ancestor is refused even when its target lies outside every known
    default SSOT location.
    """

    for index, candidate in enumerate([lexical, *lexical.parents]):
        if not os.path.lexists(candidate):
            continue
        if is_link_or_reparse(candidate):
            raise FixtureRefused(
                "symlink_or_reparse_destination"
                if index == 0
                else "symlink_or_reparse_ancestor"
            )


def refuse_destination(raw: Path) -> Path:
    """Refuse unsafe destinations before any resolve, Store construction or mkdir."""

    expanded = raw.expanduser()
    if not expanded.is_absolute():
        raise FixtureRefused("relative_destination")
    if any(part == ".." for part in expanded.parts):
        raise FixtureRefused("non_literal_destination")
    if is_live_ssot(expanded):
        raise FixtureRefused("live_ssot_destination")
    refuse_alias_chain(expanded)
    if os.path.lexists(expanded):
        if not expanded.is_dir():
            raise FixtureRefused("destination_not_a_directory")
        with os.scandir(expanded) as entries:
            if any(entries):
                raise FixtureRefused("existing_nonempty_destination")
    resolved = expanded.resolve()
    if is_live_ssot(resolved):
        raise FixtureRefused("live_ssot_destination")
    return resolved


@contextmanager
def isolated_runtime(destination: Path) -> Iterator[Path]:
    runtime = destination / RUNTIME_DIRNAME
    previous = os.environ.get("WORK_STACK_RUNTIME")
    os.environ["WORK_STACK_RUNTIME"] = str(runtime)
    try:
        yield runtime
    finally:
        if previous is None:
            os.environ.pop("WORK_STACK_RUNTIME", None)
        else:
            os.environ["WORK_STACK_RUNTIME"] = previous


class _UuidSequence:
    def __init__(self) -> None:
        self._index = 0

    def uuid4(self) -> uuid.UUID:
        self._index += 1
        return uuid.uuid5(UUID_NAMESPACE, "dogfood-uuid4-{}".format(self._index))


@contextmanager
def freeze_generation() -> Iterator[None]:
    sequence = _UuidSequence()
    with mock.patch("uuid.uuid4", sequence.uuid4), mock.patch(
        "workstack.service.today", return_value=TODAY
    ), mock.patch(
        "workstack.service.utc_now", return_value=NOW
    ), mock.patch(
        # Status notices read this helper, not service.utc_now. Leaving it on
        # the wall clock made v3 activity.json differ across destinations
        # when create_fixture crossed a UTC second (13:20:56Z vs 13:20:57Z).
        "workstack.mutation_service._utc_now",
        return_value=NOW,
    ):
        yield


def _ref(objective_id: str, key_result_id: str) -> dict[str, str]:
    return {"objective_id": objective_id, "key_result_id": key_result_id}


def populate_stack(stack: WorkStack, *, include_key_result_refs: bool) -> dict[str, Any]:
    """Seed the released WorkStack APIs with the compact dogfood dataset."""

    compact = stack.add_objective(OBJECTIVE_COMPACT, QUARTER)
    review = stack.add_objective(OBJECTIVE_REVIEW, QUARTER)
    linked = stack.add_key_result(compact["id"], KR_LINKED)
    stack.add_key_result(compact["id"], KR_ZERO)
    stack.add_key_result(review["id"], KR_REVIEW)
    done = stack.add_task(TASK_DONE, objective_ids=[compact["id"]])
    blocked = stack.add_task(
        TASK_BLOCKED,
        objective_ids=[compact["id"]],
        dependencies=[done["id"]],
    )
    free = stack.add_task(TASK_FREE, objective_ids=[review["id"]])
    if include_key_result_refs:
        pair = _ref(compact["id"], linked["id"])
        done = stack.patch_task(
            done["id"],
            {
                "key_result_refs": [pair],
                "revision": stack.get_task(done["id"])["revision"],
            },
        )
        blocked = stack.patch_task(
            blocked["id"],
            {
                "key_result_refs": [pair],
                "revision": stack.get_task(blocked["id"])["revision"],
            },
        )
    done = stack.set_task_status(done["id"], "done")
    note = stack.add_note(NOTE_TEXT, links=[blocked["id"], compact["id"]])
    checkpoint = stack.add_worklog(
        free["id"],
        done=[CHECKPOINT_DONE],
        next_items=[CHECKPOINT_NEXT],
    )
    return semantic_inventory(stack, note_id=note["id"], checkpoint=checkpoint)


def semantic_inventory(
    stack: WorkStack,
    *,
    note_id: str | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks = stack.list_tasks(status="all")
    objectives = stack.list_objectives(status="all")
    notes = stack.store.load("notes.json").get("notes", [])
    worklog = stack.list_worklog()
    context = project_context_items(
        notes,
        [],
        [task["id"] for task in tasks],
        [item["id"] for item in objectives],
    )
    by_title = {task["title"]: task for task in tasks}
    return {
        "workspace_id": stack.store.load("workspace.json")["id"],
        "objectives": [
            {
                "id": item["id"],
                "objective": item["objective"],
                "key_results": [
                    {"id": child["id"], "text": child["text"]}
                    for child in item.get("key_results", [])
                ],
            }
            for item in objectives
        ],
        "tasks": [
            {
                "id": task["id"],
                "title": task["title"],
                "status": task["status"],
                "objective_ids": list(task.get("objective_ids", [])),
                "dependencies": list(task.get("dependencies", [])),
                "key_result_refs": list(task.get("key_result_refs", [])),
            }
            for task in tasks
        ],
        "note": next((item for item in notes if item["id"] == note_id), notes[0] if notes else None),
        "checkpoint": checkpoint or _checkpoint_from_worklog(worklog, by_title[TASK_FREE]["id"]),
        "context_item_count": len(context),
        "format_meanings": dict(FORMAT_MEANINGS),
        "roles": {
            "done_prerequisite": by_title[TASK_DONE]["id"],
            "blocked_reveal": by_title[TASK_BLOCKED]["id"],
            "selected_without_prerequisites": by_title[TASK_FREE]["id"],
        },
    }


def _checkpoint_from_worklog(worklog: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    days = worklog.get("days", {})
    for date, day in days.items():
        for entry in day.get("entries", []):
            if entry.get("task_id") == task_id:
                return {"date": date, **entry}
    raise FixtureRefused("checkpoint_missing")


def _write_v4_conversion(root: Path, conversion: Any) -> None:
    def write(relative: str, body: bytes) -> None:
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    write("store.json", canonical_json_bytes(dict(conversion.store)))
    write("workspace.json", canonical_json_bytes(dict(conversion.workspace)))
    for kind, records in conversion.records.items():
        for record in records:
            uid = str(record["uid"])
            write(
                "records/{}/{}/{}.json".format(kind, uid[:2], uid),
                canonical_json_bytes(dict(record)),
            )
    segments: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for kind, events in conversion.streams.items():
        for event in events:
            segments.setdefault((kind, str(event["created_at"])[:7]), []).append(dict(event))
    for (kind, month), events in sorted(segments.items()):
        body = b"".join(
            canonical_json_bytes(event) + b"\n"
            for event in sorted(events, key=lambda item: item["sequence"])
        )
        write("streams/{}/{}.ndjson".format(kind, month), body)


def _step_back_to_v3(bodies: dict[str, bytes]) -> dict[str, bytes]:
    """Return the historical v3 authority the nine held payloads already are.

    Schema 5 widened the *roster* -- it added reports.json -- and schema 6
    widened it again with knowledge.json, and both left the nine v3 payload
    shapes untouched. So the only document that still says something a v3 store
    never said is the metadata record: it carries the current schema and the
    evidence entries historical v3 has no field for. Stepping those facts back
    is the whole difference, and the result is judged genuine v3 by the
    released validators rather than merely labelled as such. Nothing here
    loosens a product reader: the frozen v3 source roster still decides which
    documents exist at all.
    """

    metadata = json.loads(bodies["store-meta.json"].decode("utf-8"))
    metadata["store_schema_version"] = 3
    for name in ("reports", "knowledge"):
        metadata["migrations"].pop(name, None)
    return {**bodies, "store-meta.json": _serialized_json_bytes(metadata)}


def _generate_v3(*, include_key_result_refs: bool) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Seed the released APIs in disposable staging; return v3 bytes and semantics.

    The released Store this build ships writes the current schema, so the
    dataset is produced by the real product APIs and only then stepped back to
    the frozen v3 roster. Staging is a private temporary directory, so the
    caller's destination never holds a current-schema document, a lock file or
    a runtime directory even for an instant.
    """

    with tempfile.TemporaryDirectory(prefix="dogfood-source-") as staging_name:
        staging = Path(staging_name)
        with isolated_runtime(staging), freeze_generation():
            stack = WorkStack(Store(staging))
            inventory = populate_stack(
                stack, include_key_result_refs=include_key_result_refs
            )
        bodies = {name: (staging / name).read_bytes() for name in V3_SOURCE_FILES}
    return _step_back_to_v3(bodies), inventory


def _write_v3_documents(destination: Path, bodies: Mapping[str, bytes]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in V3_SOURCE_FILES:
        (destination / name).write_bytes(bodies[name])


def _create_v3(destination: Path, *, include_key_result_refs: bool) -> dict[str, Any]:
    bodies, inventory = _generate_v3(include_key_result_refs=include_key_result_refs)
    _write_v3_documents(destination, bodies)
    inventory["format"] = "v3" if include_key_result_refs else "v3-source-for-v4"
    inventory["store_schema_version"] = 3
    inventory["destination"] = str(destination)
    inventory["key_result_refs_admitted"] = include_key_result_refs
    return inventory


def _create_v4(destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    bodies, source = _generate_v3(include_key_result_refs=False)
    documents = {
        name: json.loads(body.decode("utf-8")) for name, body in bodies.items()
    }
    conversion = convert_v3_documents(documents, candidate_created_at=NOW)
    _write_v4_conversion(destination, conversion)
    return {
        "format": "v4",
        "source_store_schema_version": 3,
        "destination": str(destination),
        "workspace_id": source["workspace_id"],
        "objectives": source["objectives"],
        "tasks": [
            {key: value for key, value in task.items() if key != "key_result_refs"}
            for task in source["tasks"]
        ],
        "note": source["note"],
        "checkpoint": source["checkpoint"],
        "roles": source["roles"],
        "format_meanings": dict(FORMAT_MEANINGS),
        "key_result_refs_admitted": False,
        "key_result_refs_note": (
            "Experimental v4 has no Task key_result_refs field. "
            "Released v3 admits scoped refs; this option is a separate conversion "
            "of a v3 source that never persisted that field."
        ),
    }


def create_fixture(destination: Path, storage_format: str) -> dict[str, Any]:
    if storage_format not in {"v3", "v4"}:
        raise FixtureRefused("unsupported_format")
    resolved = refuse_destination(destination)
    if storage_format == "v3":
        return _create_v3(resolved, include_key_result_refs=True)
    return _create_v4(resolved)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Create a disposable synthetic Work Stack dogfood fixture."
    )
    root.add_argument(
        "--out",
        required=True,
        help=(
            "required explicit absolute destination directory (a leading ~ is "
            "expanded first); must be absent or empty, must contain no '..' "
            "segment, must not be or sit under a link/junction/reparse point, "
            "and must never be a live SSOT path"
        ),
    )
    root.add_argument(
        "--format",
        choices=("v3", "v4"),
        default="v3",
        help=(
            "v3 writes a historical schema-3 authority keeping the scoped KR "
            "linkage; v4 converts a separate historical schema-3 source that "
            "never persisted that field"
        ),
    )
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        receipt = create_fixture(Path(arguments.out), arguments.format)
    except FixtureRefused as error:
        sys.stderr.write("{}\n".format(error.code))
        return 2
    json.dump(receipt, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
