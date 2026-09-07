"""Pure v4 stream rewrite for a later permanent-delete transaction.

Filesystem-free. Rebuilds the combined global digest chain after removing
target-owned events. Does not append, acquire a lease, or call a write session.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .canonical import CanonicalJsonError, canonical_json_bytes, canonical_sha256
from .contracts import StorageContractError, require_valid_by_format
from .journal import JournalTarget
from .layout import STREAM_KINDS


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORMAT_BY_KIND = {
    "activity": "workstack.activity-event",
    "planning-status": "workstack.planning-status-event",
    "worklog": "workstack.worklog-event",
}


class V4StreamRewriteError(ValueError):
    """Stable, content-free refusal to rewrite a v4 stream chain."""

    def __init__(self, code: str = "invalid_stream") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class V4StreamDeletion:
    """Exact plan identities for events that must leave the global chain."""

    activity_ids: tuple[str, ...]
    planning_ids: tuple[str, ...]
    worklog_entries: tuple[tuple[str, str], ...]
    worklog_sessions: tuple[tuple[str, str, str], ...]


def _fail() -> None:
    raise V4StreamRewriteError("invalid_stream")


def _require(condition: bool) -> None:
    if not condition:
        _fail()


def _sha256(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _segment_artifact(kind: str, created_at: object) -> str:
    _require(kind in STREAM_KINDS)
    _require(isinstance(created_at, str) and len(created_at) >= 7)
    month = created_at[:7]
    _require(len(month) == 7 and month[4] == "-")
    return f"streams/{kind}/{month}.ndjson"


def _segment_body(events: Sequence[Mapping[str, Any]]) -> bytes:
    return b"\n".join(canonical_json_bytes(dict(event)) for event in events) + b"\n"


def _text(value: object) -> str:
    _require(isinstance(value, str) and value)
    return value


def _sequence_of(event: Mapping[str, Any]) -> int:
    value = event.get("sequence")
    _require(type(value) is int and value >= 1)
    return value


def _copy_event(event: Mapping[str, Any]) -> dict[str, Any]:
    _require(isinstance(event, Mapping))
    return copy.deepcopy(dict(event))


def _unknown_kinds(streams: Mapping[str, Any]) -> None:
    _require(isinstance(streams, Mapping))
    extra = set(streams) - set(STREAM_KINDS)
    _require(not extra)


def _kind_events(streams: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    raw = streams.get(kind, ())
    _require(isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)))
    events: list[Mapping[str, Any]] = []
    for item in raw:
        _require(isinstance(item, Mapping))
        events.append(item)
    return events


def _validate_event_contract(kind: str, event: Mapping[str, Any]) -> None:
    _require(event.get("format") == _FORMAT_BY_KIND[kind])
    try:
        require_valid_by_format(dict(event))
        canonical_json_bytes(dict(event))
    except (CanonicalJsonError, StorageContractError, TypeError, ValueError) as error:
        raise V4StreamRewriteError("invalid_stream") from error


def _collect_entries(
    streams: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    _unknown_kinds(streams)
    entries: list[tuple[str, Mapping[str, Any]]] = []
    for kind in STREAM_KINDS:
        for event in _kind_events(streams, kind):
            _validate_event_contract(kind, event)
            entries.append((kind, event))
    entries.sort(key=lambda item: _sequence_of(item[1]))
    return entries


def _require_contiguous(entries: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
    actual = [_sequence_of(event) for _, event in entries]
    _require(actual == list(range(1, len(entries) + 1)))


def _claimed_digest(event: Mapping[str, Any]) -> str:
    claimed = event.get("event_digest")
    _require(isinstance(claimed, str) and _SHA256.fullmatch(claimed) is not None)
    return claimed


def _digest_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    payload.pop("event_digest", None)
    return payload


def _require_digest_chain(entries: Sequence[tuple[str, Mapping[str, Any]]]) -> None:
    seen: set[str] = set()
    previous: str | None = None
    for _, event in entries:
        uid = _text(event.get("event_uid"))
        _require(uid not in seen)
        seen.add(uid)
        claimed = _claimed_digest(event)
        payload = _digest_payload(event)
        expected_previous = payload.get("previous_event_digest")
        _require(expected_previous == previous)
        try:
            expected = canonical_sha256(payload)
        except (CanonicalJsonError, TypeError, ValueError) as error:
            raise V4StreamRewriteError("invalid_stream") from error
        _require(claimed == expected)
        previous = claimed


def _planning_events(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[Mapping[str, Any]]:
    return [event for kind, event in entries if kind == "planning-status"]


def _require_input_planning_chain(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
) -> None:
    heads: dict[str, Mapping[str, Any]] = {}
    seen_legacy: set[str] = set()
    for index, event in enumerate(_planning_events(entries), start=1):
        legacy = _text(event.get("legacy_fact_id"))
        _require(legacy not in seen_legacy)
        seen_legacy.add(legacy)
        _require(legacy == f"PS-{index:06d}")
        task_uid = _text(event.get("task_uid"))
        previous = heads.get(task_uid)
        if previous is None:
            _require_bootstrap(event)
        else:
            _require_transition(event, previous)
        heads[task_uid] = event


def _is_null(value: object) -> bool:
    return value is None


def _require_bootstrap(event: Mapping[str, Any]) -> None:
    fields = ("previous_event_uid", "previous_legacy_fact_id", "prior_revision", "prior_status")
    _require(all(_is_null(event.get(field)) for field in fields))


def _require_transition(event: Mapping[str, Any], previous: Mapping[str, Any]) -> None:
    _require(event.get("previous_event_uid") == previous.get("event_uid"))
    _require(event.get("previous_legacy_fact_id") == previous.get("legacy_fact_id"))
    _require(event.get("prior_status") == previous.get("status"))
    prior_revision = event.get("prior_revision")
    new_revision = event.get("new_revision")
    previous_revision = previous.get("new_revision")
    _require(type(prior_revision) is int)
    _require(type(new_revision) is int)
    _require(type(previous_revision) is int)
    _require(new_revision == prior_revision + 1)
    _require(prior_revision >= previous_revision)
    _require(event.get("status") != previous.get("status"))


def _ordered_existing(
    streams: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    entries = _collect_entries(streams)
    _require_contiguous(entries)
    _require_digest_chain(entries)
    _require_input_planning_chain(entries)
    return entries


def _unique_texts(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        text = _text(item)
        _require(text not in seen)
        seen.add(text)
        ordered.append(text)
    return tuple(ordered)


def _unique_pairs(values: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for item in values:
        _require(isinstance(item, tuple) and len(item) == 2)
        pair = (_text(item[0]), _text(item[1]))
        _require(pair not in seen)
        seen.add(pair)
        ordered.append(pair)
    return tuple(ordered)


def _unique_triples(
    values: Sequence[tuple[str, str, str]],
) -> tuple[tuple[str, str, str], ...]:
    seen: set[tuple[str, str, str]] = set()
    ordered: list[tuple[str, str, str]] = []
    for item in values:
        _require(isinstance(item, tuple) and len(item) == 3)
        triple = (_text(item[0]), _text(item[1]), _text(item[2]))
        _require(triple not in seen)
        seen.add(triple)
        ordered.append(triple)
    return tuple(ordered)


def _normalized_deletion(deletion: V4StreamDeletion) -> V4StreamDeletion:
    return V4StreamDeletion(
        activity_ids=_unique_texts(deletion.activity_ids),
        planning_ids=_unique_texts(deletion.planning_ids),
        worklog_entries=_unique_pairs(deletion.worklog_entries),
        worklog_sessions=_unique_triples(deletion.worklog_sessions),
    )


def _activity_identity(event: Mapping[str, Any]) -> set[str]:
    keys = set()
    legacy = event.get("legacy_event_id")
    if isinstance(legacy, str) and legacy:
        keys.add(legacy)
    keys.add(_text(event.get("event_uid")))
    return keys


def _planning_identity(event: Mapping[str, Any]) -> set[str]:
    keys = set()
    legacy = event.get("legacy_fact_id")
    if isinstance(legacy, str) and legacy:
        keys.add(legacy)
    keys.add(_text(event.get("event_uid")))
    return keys


def _mark_kind(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
    kind: str,
    wanted: Sequence[str],
    identities: Any,
) -> set[int]:
    remaining = set(wanted)
    selected: set[int] = set()
    for index, (event_kind, event) in enumerate(entries):
        if event_kind != kind:
            continue
        matched = identities(event) & remaining
        if not matched:
            continue
        _require(len(matched) == 1)
        _require(index not in selected)
        selected.add(index)
        remaining.difference_update(matched)
    _require(not remaining)
    return selected


def _mark_worklog_entries(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
    wanted: Sequence[tuple[str, str]],
) -> set[int]:
    remaining = set(wanted)
    selected: set[int] = set()
    for index, (kind, event) in enumerate(entries):
        if kind != "worklog" or event.get("kind") != "entry":
            continue
        key = (_text(event.get("event_uid")), _text(event.get("work_date")))
        if key not in remaining:
            continue
        _require(index not in selected)
        selected.add(index)
        remaining.remove(key)
    _require(not remaining)
    return selected


def _mark_worklog_sessions(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
    wanted: Sequence[tuple[str, str, str]],
) -> set[int]:
    remaining = set(wanted)
    selected: set[int] = set()
    for index, (kind, event) in enumerate(entries):
        if kind != "worklog" or event.get("kind") != "session":
            continue
        key = (
            _text(event.get("event_uid")),
            _text(event.get("work_date")),
            _text(event.get("session_id")),
        )
        if key not in remaining:
            continue
        _require(index not in selected)
        selected.add(index)
        remaining.remove(key)
    _require(not remaining)
    return selected


def _selected_indexes(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
    deletion: V4StreamDeletion,
) -> set[int]:
    selected = _mark_kind(entries, "activity", deletion.activity_ids, _activity_identity)
    planning = _mark_kind(
        entries, "planning-status", deletion.planning_ids, _planning_identity
    )
    _require(selected.isdisjoint(planning))
    selected.update(planning)
    entries_marked = _mark_worklog_entries(entries, deletion.worklog_entries)
    _require(selected.isdisjoint(entries_marked))
    selected.update(entries_marked)
    sessions = _mark_worklog_sessions(entries, deletion.worklog_sessions)
    _require(selected.isdisjoint(sessions))
    selected.update(sessions)
    return selected


def _remaining_entries(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
    selected: set[int],
) -> list[tuple[str, dict[str, Any]]]:
    remaining: list[tuple[str, dict[str, Any]]] = []
    for index, (kind, event) in enumerate(entries):
        if index in selected:
            continue
        remaining.append((kind, _copy_event(event)))
    return remaining


def _apply_bootstrap(event: dict[str, Any]) -> None:
    event["previous_event_uid"] = None
    event["previous_legacy_fact_id"] = None
    event["prior_revision"] = None
    event["prior_status"] = None


def _apply_transition(event: dict[str, Any], previous: Mapping[str, Any]) -> None:
    event["previous_event_uid"] = previous["event_uid"]
    event["previous_legacy_fact_id"] = previous["legacy_fact_id"]
    if event.get("prior_status") != previous.get("status"):
        event["prior_status"] = previous.get("status")
    if event.get("prior_revision") != previous.get("new_revision"):
        if event.get("new_revision") == previous.get("new_revision") + 1:
            event["prior_revision"] = previous.get("new_revision")
    try:
        _require_transition(event, previous)
    except V4StreamRewriteError:
        _fail()


def _repair_planning(entries: list[tuple[str, dict[str, Any]]]) -> None:
    heads: dict[str, dict[str, Any]] = {}
    index = 0
    for kind, event in entries:
        if kind != "planning-status":
            continue
        index += 1
        event["legacy_fact_id"] = f"PS-{index:06d}"
        task_uid = _text(event.get("task_uid"))
        previous = heads.get(task_uid)
        if previous is None:
            _apply_bootstrap(event)
        else:
            _apply_transition(event, previous)
        heads[task_uid] = event


def _rechain(entries: Sequence[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    previous: str | None = None
    chained: list[tuple[str, dict[str, Any]]] = []
    for sequence, (kind, source) in enumerate(entries, start=1):
        event = _copy_event(source)
        event.pop("event_digest", None)
        event.pop("previous_event_digest", None)
        event["sequence"] = sequence
        if previous is not None:
            event["previous_event_digest"] = previous
        try:
            event["event_digest"] = canonical_sha256(event)
            _validate_event_contract(kind, event)
        except (CanonicalJsonError, TypeError, ValueError, V4StreamRewriteError) as error:
            raise V4StreamRewriteError("invalid_stream") from error
        previous = str(event["event_digest"])
        chained.append((kind, event))
    return chained


def _group_segments(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for kind, event in entries:
        artifact = _segment_artifact(kind, event.get("created_at"))
        grouped.setdefault(artifact, []).append(event)
    return grouped


def _require_digest_value(value: object) -> str:
    _require(isinstance(value, str) and _SHA256.fullmatch(value) is not None)
    return value


def _emit_segment(
    artifact: str,
    original: Sequence[Mapping[str, Any]],
    rewritten: Sequence[Mapping[str, Any]],
    segment_digests: Mapping[str, str],
) -> JournalTarget | None:
    original_body = _segment_body(original) if original else None
    rewritten_body = _segment_body(rewritten) if rewritten else None
    if original_body == rewritten_body:
        return None
    _require(original)
    expected = _require_digest_value(segment_digests.get(artifact))
    actual = _sha256(original_body or b"")
    _require(expected == actual)
    if not rewritten:
        return JournalTarget.delete(artifact, expected_digest=expected, scope="authority")
    return JournalTarget.replace(
        artifact,
        rewritten_body or b"",
        expected_digest=expected,
        scope="authority",
    )


def _sorted_targets(targets: Iterable[JournalTarget]) -> tuple[JournalTarget, ...]:
    items = list(targets)
    artifacts = [item.artifact for item in items]
    _require(len(artifacts) == len(set(artifacts)))
    items.sort(key=lambda item: (item.scope, item.artifact, item.action))
    return tuple(items)


def rewrite_v4_deletion_streams(
    streams: Mapping[str, Sequence[Mapping[str, Any]]],
    deletion: V4StreamDeletion,
    *,
    segment_digests: Mapping[str, str],
) -> tuple[JournalTarget, ...]:
    """Rebuild the global v4 stream chain after removing exact plan identities.

    Returns CAS replace/delete targets for changed segments only. Input stream
    mappings are not mutated. Mapping iteration order does not affect bytes.
    """

    _require(isinstance(segment_digests, Mapping))
    _require(isinstance(streams, Mapping))
    snapshot = {
        kind: [_copy_event(event) for event in _kind_events(streams, kind)]
        for kind in STREAM_KINDS
    }
    for kind in list(streams):
        if kind not in STREAM_KINDS:
            _fail()
    original = _ordered_existing(snapshot)
    wanted = _normalized_deletion(deletion)
    selected = _selected_indexes(original, wanted)
    remaining = _remaining_entries(original, selected)
    _repair_planning(remaining)
    chained = _rechain(remaining)
    original_segments = _group_segments(original)
    rewritten_segments = _group_segments(chained)
    artifacts = set(original_segments) | set(rewritten_segments)
    emitted: list[JournalTarget] = []
    for artifact in sorted(artifacts):
        target = _emit_segment(
            artifact,
            original_segments.get(artifact, ()),
            rewritten_segments.get(artifact, ()),
            segment_digests,
        )
        if target is not None:
            emitted.append(target)
    return _sorted_targets(emitted)
