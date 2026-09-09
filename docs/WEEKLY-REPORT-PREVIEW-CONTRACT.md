# Weekly report preview contract (O2 HTTP)

Status: implemented core and read-only loopback
`GET /api/v1/reports/weekly-preview`. Product CLI, draft persistence,
scheduler, editor, and finalize paths are not attached.

## Function

```python
from workstack.weekly_reporting import (
    TEMPLATE_WEEKLY_V1,  # "weekly-v1"
    WeeklyReportPreviewError,
    preview_weekly_report,
)

preview = preview_weekly_report(
    projection=review_projection,  # WorkStack.review_projection(end_date, 7)
    end_date="2026-08-30",         # explicit civil end day, YYYY-MM-DD
    template=TEMPLATE_WEEKLY_V1,
    generated_at="2026-09-06T01:02:03Z",  # RFC 3339; preserved verbatim
)
```

The function is pure. The HTTP adapter loads one review projection and the
admitted Captures under the same `Store.consistent_read`, then passes the
projection through. The core never reads documents, never writes, and never
calls `WorkStack.review_projection`.

## HTTP adapter

```python
from workstack.weekly_reporting_http import (
    weekly_preview_payload,
    weekly_source_digest,
    parse_weekly_preview_query,
)
```

`weekly_source_digest(end_date=..., weekly=projection["weekly"])` is
`capture.canonical_digest({"end_date": end_date, "weekly": weekly})` and
returns `sha256:` plus 64 lowercase hex. It never includes `generated_at`,
`day`, a prior preview, or Markdown.

## Product HTTP (loopback GET)

Frontend consumes this path exactly. It is a read-only owner GET on the
existing loopback server: same Host / session / origin / access boundary as
other `GET /api/v1/*` routes. It does not open a new listener, does not
require CSRF, and does not bypass `store_sync_required`.

```
GET /api/v1/reports/weekly-preview?end_date=YYYY-MM-DD&template=weekly-v1&workspace_uid=<canonical UUID>
```

Query keys are exactly `end_date`, `template`, and `workspace_uid`, each
once. Missing, blank, duplicate, or unknown keys are refused. `end_date`
must be a canonical calendar `YYYY-MM-DD`. `template` must be exactly
`weekly-v1`. `workspace_uid` must be a non-nil lowercase canonical RFC 4122
UUID.

Handler order is exact:

1. Parse the query.
2. Stamp `generated_at` as UTC RFC 3339 seconds with `Z`.
3. Bind owner identity to already-admitted `store.readiness.workspace_uid`.
4. Inspect `sync_status` (precheck).
5. Enter `Store.consistent_read`.
6. Re-inspect sync, then compare snapshot UID.
7. Call `review_projection(end_date, 7)` exactly once, then
   `list_captures(status="all")` under the same held read.
8. Re-inspect sync.
9. Render the core preview and `weekly_source_digest` from that same
   projection, then `build_context_catalog` from that preview's
   `provenance.task_ids` (the week's explicit Task IDs, not the end day
   alone).

Missing admitted readiness is `422` `report_preview_unavailable`. A requested
UUID that does not match that admitted owner is `409` `workspace_mismatch`
and does not inspect the on-disk candidate. A matching request then inspects
`sync_status`; any non-`in-sync` state is `409` `store_sync_required`. Under
the held read, a valid in-window workspace-id swap is `store_sync_required`
rather than `workspace_mismatch`. `StoreCorruptError` from the held read is
caught alone: the handler re-inspects sync and maps a proven non-`in-sync`
state through the same `409` helper; any still-in-sync corruption is
re-raised. It does not adopt, initialize, rebind, or write.

`WeeklyReportPreviewError` from the core, including an unrepresentable week
start at the civil-calendar floor, is `422` `report_preview_unavailable`
with a stable message and empty `details`. A representable final civil week
(`end_date=9999-12-31`) is accepted by the same core rules.

Success `200` key order is exact:

```json
{
  "data": {
    "workspace_uid": "<actual owning Store UUID>",
    "source_digest": "sha256:<64 lowercase hex>",
    "preview": { },
    "context_catalog": {
      "captured_at": "<same generated_at as preview>",
      "items": [
        {
          "capture_id": "C-0001",
          "capture_revision": 1,
          "title": "Synthetic context",
          "linked_task_ids": ["T-0001"],
          "status": "linked"
        }
      ],
      "omitted_count": 0
    }
  }
}
```

`preview` is the exact `preview_weekly_report` object. Empty active weeks
use `"absence": "no records"` and never `"no work"`.

`context_catalog` is a sibling of `preview`, not a field inside it. The
seven-day projection and Captures are read under the same
`Store.consistent_read` snapshot already used for the preview.
`captured_at` copies `preview.generated_at`. Each item is a Capture whose
current explicit `linked_task_ids` intersect `preview.provenance.task_ids`
(unique, natural-sorted). That provenance lists the week's Task IDs, not
only the end day's. Converted provenance alone is not a link. Unrelated
and unlinked rows are omitted. Empty weeks yield empty `items`. At most 32
items appear, in natural Capture ID order, with `omitted_count` naming
additional qualifying Captures. The actual Capture status is included, so
a dismissed Capture that still has explicit links is not shown as active.
Titles are the admitted Capture `source.display_title` strings. The
catalogue is not part of `preview.markdown`, copy output, saved report
create/revise/finalize payloads, `provenance`, `source_digest`, or
`reports.json`.

`source_digest` is computed only after the core accepts the projection. Same
weekly facts at a different server clock yield the same digest. An end-day
check-in that does not change `weekly` yields the same digest. A changed
weekly project, objective, fact, or requested week range changes it.
Workspace identity stays the sibling `workspace_uid` field. Error responses
do not include `source_digest`.

| Status | `error.code` | When |
| --- | --- | --- |
| 400 | `invalid_query` | Missing/blank/duplicate/unknown keys, non-canonical date or UUID, unsupported template. Stable message. No raw query text, no traceback, no `details` payload. |
| 409 | `workspace_mismatch` | Canonical UUID does not match the owning Store. Checked before the report read. |
| 409 | `store_sync_required` | Owning Store is not `in-sync`. Same envelope as other owner routes (`state`, `generation`, `changed_files`). |
| 400 | `invalid_host` | Inherited Host boundary. |
| 422 | `report_preview_unavailable` | Core bounded refusal of an otherwise valid query (oversize/malformed after `review_projection(end_date, 7)`, including unrepresentable week start). Stable message. No raw data and empty `details`. |

This packet does not persist a draft. A later packet owns draft/UI follow-up.

## Out of scope

Product CLI wiring, draft persistence, AI generation, scheduling, an
editor, and finalize/publish all belong to later owners.
