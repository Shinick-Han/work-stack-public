"""Owner-aware writer transport for CLI mutations.

The T-0002 vertical slices. When the local data directory carries running owner
metadata, these commands must not take the exclusive local Store path: the
running server owns the lease and a direct write would either fail or create a
second writer. This module is the public facade: owner binding, shared
forward transport, and domain response validation live in ``cli_writer_*``
collaborators. Public names, exception identities, and injected
``request_json`` / ``coordinates_reader`` seams stay identical.

Failure is closed by construction. Every error raised here propagates to the CLI
top level, which reports exit 2. No path in this package falls back to a local
write, and none removes or repairs owner metadata.
"""

from __future__ import annotations

import datetime
from urllib.parse import quote  # noqa: F401  - checkpoint_state_cli uses cli_writer.quote

from .cli_writer_owner import (  # noqa: F401  - public compatibility surface
    AMBIGUOUS_TRANSPORT,
    CoordinatesReader,
    LOOPBACK_HOSTS,
    OWNER_ABSENT,
    OWNER_INVALID,
    OWNER_PRESENT,
    RequestJson,
    SERVER_INFO_READ_LIMIT,
    STORE_MANIFEST_READ_LIMIT,
    SYNC_STATES,
    CommitUnknownError,
    WriterTransportError,
    _canonical_workspace_uid,
    _origin,
    _preflight,
    _preflight_get,
    _read_bounded,
    _resolve_coordinates,
    bind_open as _bind_open,
    expected_workspace_uid,
    owner_metadata_state,
    read_owner_binding,
)
from .cli_writer_planning import (  # noqa: F401
    LEGACY_KEY_RESULT_FIELDS,
    LEGACY_OBJECTIVE_FIELDS,
    NOTES_PATH,
    OBJECTIVES_PATH,
    _created_key_result_id,
    _key_result_from,
    _note_from,
    _objective_detail,
    _objective_from,
    _require_incremented_revision,
    _responded_objective,
    _scoped_key_result_ids,
    _valid_created_key_result,
    forward_key_result,
    forward_note,
    forward_objective,
)
from .cli_writer_records import (  # noqa: F401
    _backlog_add_result,
    _backlog_collections_match,
    _backlog_identity_matches,
    _backlog_values_match,
    _checkin_result,
    _cli_calendar_date,
    _cli_record_uid,
    _cli_result_data,
    _cli_string_list,
    _okr_link_identity_matches,
    _okr_link_result,
    _okr_progress_result,
    _worklog_entry_categories_match,
    _worklog_entry_result,
    bind_clock_provider as _bind_clock_provider,
    forward_backlog_add,
    forward_checkin,
    forward_okr_link,
    forward_okr_progress,
    forward_worklog_entry,
)
from .cli_writer_subtasks import (  # noqa: F401
    PROJECTION_INJECTED_NONE,
    SANCTIONED_APPEND_EFFECTS,
    SUBTASK_ID,
    SUBTASK_KEYS,
    SUBTASK_STATUS_PARENT_EFFECTS,
    _appended_subtask,
    _located_subtask,
    _parent_survived_subtask_status,
    _parent_values_preserved,
    _revision_of,
    _subtask_baseline,
    _subtask_from,
    _subtask_status_from,
    _valid_created_subtask,
    forward_subtask,
    forward_subtask_status,
)
from .cli_writer_tasks import (  # noqa: F401
    LEGACY_TASK_NOTE_FIELDS,
    PROJECTED_TASK_FIELDS,
    TASK_STATUS_VALUES,
    TASKS_PATH,
    _complete_projected_task,
    _same_json,
    _task_detail,
    _task_note_baseline,
    _task_note_from,
    _task_status_from,
    _valid_created_note,
    forward_task_note,
    forward_task_status,
)
from .cli_writer_transport import (  # noqa: F401
    _forward_write,
    _validate_keyless_post,
    _write_headers,
    new_idempotency_key,
)
from .outcome_write_invariant import serialize_task_patch  # noqa: F401
from .store import MAX_REVISION  # noqa: F401  (constant only; no Store is built)

# Honor tests that patch ``cli_writer.open`` / ``cli_writer.datetime``.
_bind_open(lambda *args, **kwargs: open(*args, **kwargs))
_bind_clock_provider(lambda: datetime)
