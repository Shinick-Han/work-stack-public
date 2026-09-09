# Daily report preview contract (O2 core)

Status: implemented core, standalone stdin CLI, and read-only loopback
`GET /api/v1/reports/daily-preview`. Product CLI, draft persistence,
scheduler, editor, and finalize paths are not attached.

## Function

```python
from workstack.reporting import (
    TEMPLATE_DAILY_V1,  # "daily-v1"
    DailyReportPreviewError,
    preview_daily_report,
)

preview = preview_daily_report(
    projection=review_projection,  # already-validated WorkStack.review_projection result
    date="2026-08-30",             # explicit civil day, YYYY-MM-DD
    template=TEMPLATE_DAILY_V1,
    generated_at="2026-09-06T01:02:03Z",  # RFC 3339; preserved verbatim
)
```

The function is pure. Callers supply a review projection they already loaded.
This module never reads documents, never writes, and never calls
`WorkStack.review_projection`.

## Inputs

| Name | Rule |
| --- | --- |
| `projection` | Mapping with `day.date` equal to `date`. `day.entries` must be a list. `weekly` is optional provenance only. |
| `date` | Canonical `YYYY-MM-DD`. Invalid or non-canonical calendar dates are refused. |
| `template` | Exactly `daily-v1`. Any other value is refused. |
| `generated_at` | RFC 3339 timestamp with `Z` or numeric offset. The string is not rewritten. |

Oversized projections (more than 262144 UTF-8 bytes, more than 200 day
entries, more than 32 items in one list, or any item longer than 1000
characters) and oversized Markdown (more than 100000 characters) are refused
with `DailyReportPreviewError`.

`DailyReportPreviewError.details["field"]` names the refused input.

## Output (JSON-compatible)

Insertion order is stable:

```json
{
  "template": "daily-v1",
  "period": { "kind": "day", "date": "2026-08-30" },
  "generated_at": "2026-09-06T01:02:03Z",
  "absence": null,
  "provenance": {
    "date": "2026-08-30",
    "task_ids": ["T-0001"],
    "sources": [
      {
        "kind": "review.day.entry",
        "date": "2026-08-30",
        "index": 0,
        "task_id": "T-0001",
        "unknown_fields": []
      }
    ],
    "weekly_range": { "start": "2026-08-24", "end": "2026-08-30", "days": 7 },
    "ignored_keys": []
  },
  "markdown": "# Daily review 2026-08-30\n..."
}
```

- `period` is the requested civil day. It is never copied from `generated_at`.
- `absence` is `"no records"` when the active day has no entries, otherwise
  `null`. The Markdown uses the same phrase. It never says "no work".
- `provenance.task_ids` is first-seen order from active day entries.
- `provenance.sources` keeps one object per projected entry, including repeats
  of the same Task. Index is the day-list position.
- `weekly_range` copies `projection.weekly.range` when that object is well
  formed. Weekly projects, counts, and durations are not treated as day facts
  and are not copied into Markdown.
- `ignored_keys` lists top-level preview-shaped keys (`template`, `markdown`,
  `generated_at`, `period`, `absence`, `provenance`) so a prior generated
  report cannot be recursively ingested.

## Active versus superseded

The preview consumes the **active** review projection. That projection is
built by `WorkStack.review_projection`, which uses the shared
`active_worklog_document` membership: superseded checkpoint rows are already
absent. This core does not restore them. An empty active day is "no records".

## Facts

Rendered only from `day.start_time` and `day.entries`:

- Check-in `null` → "Not recorded." Missing key → "Omitted." Non-string →
  "Unusable."
- Missing Task id or title → "Task omitted" / "title omitted".
- `done` / `next` / `blockers` missing → "`Field`: omitted." Unusable type →
  "`Field`: unusable." Empty recorded lists are skipped, not filled with
  invented completion.
- `session_id` and `duration_seconds` are optional recorded locators, never
  progress.
- Unknown entry keys are listed in `source.unknown_fields` and not interpreted.
- User strings are inert CommonMark/GFM: `<>&` become entities so tags
  cannot appear in the Markdown source. Every remaining ASCII punctuation
  character is backslash-escaped, matching CommonMark's punctuation set, so
  headings, lists, emphasis, strike, tables, links, `https://` autolinks,
  and `user@host` autolinks stay visible characters, not markup.

The preview does not invent Task status, percent complete, or "work happened".

## Standalone stdin CLI

```
python scripts/preview_daily_report.py --date 2026-08-30 --template daily-v1 --generated-at 2026-09-06T01:02:03Z < review_projection.json
```

The process reads at most 262145 bytes from stdin, requires one JSON object,
and writes only the preview JSON to stdout. It does not open Store, the
server, the product CLI, or the network. Invalid UTF-8, JSON, nesting,
surrogates, or oversize input exits nonzero with one generic stderr line
and no traceback.

## Product HTTP (loopback GET)

Frontend consumes this path exactly. It is a read-only owner GET on the
existing loopback server: same Host / session / origin / access boundary as
other `GET /api/v1/*` routes. It does not open a new listener, does not
require CSRF, and does not bypass `store_sync_required`.

```
GET /api/v1/reports/daily-preview?date=YYYY-MM-DD&template=daily-v1&workspace_uid=<canonical UUID>
```

Query keys are exactly `date`, `template`, and `workspace_uid`, each once.
Missing, blank, duplicate, or unknown keys are refused. `date` must be a
canonical calendar `YYYY-MM-DD`. `template` must be exactly `daily-v1`.
`workspace_uid` must be a non-nil lowercase canonical RFC 4122 UUID.

Owner identity is bound first to the already server-admitted
`store.readiness.workspace_uid`. Missing admitted readiness is `422`
`report_preview_unavailable`. A requested UUID that does not match that
admitted owner is `409` `workspace_mismatch` and does not inspect the
on-disk candidate, so poisoned or replacement bytes cannot change the
owner or leak through `StoreCorruptError`. A matching request then inspects
`sync_status`; any non-`in-sync` state is `409` `store_sync_required`
(including a malformed candidate or an external workspace-id replacement
while the caller still presents the original owner). Only then does the
handler enter `Store.consistent_read`. Under that held read it inspects
sync again *before* comparing snapshot UID and *after* `review_projection(date, 1)`,
so a valid in-window workspace-id swap is `store_sync_required` rather than
`workspace_mismatch`. `StoreCorruptError` from the held read is caught
alone: the handler re-inspects sync and maps a proven non-`in-sync` state
through the same `409` helper; any still-in-sync corruption is re-raised.
It does not adopt, initialize, rebind, or write. `generated_at` is
the server's UTC RFC 3339 instant (`Z`) and is passed through the core
verbatim.

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

`preview` is the exact `preview_daily_report` object (same key order and
values as the core). Empty active days use `"absence": "no records"` and
never `"no work"`.

`context_catalog` is a sibling of `preview`, not a field inside it. Day and
Captures are read under the same `Store.consistent_read` snapshot already
used for the preview. `captured_at` copies `preview.generated_at`. Each item
is a Capture whose current explicit `linked_task_ids` intersect
`preview.provenance.task_ids` (unique, natural-sorted). Converted provenance
alone is not a link. Unrelated and unlinked rows are omitted. Empty days
yield empty `items`. At most 32 items appear, in natural Capture ID order,
with `omitted_count` naming additional qualifying Captures. The actual
Capture status is included, so a dismissed Capture that still has explicit
links is not shown as active. Titles are the admitted Capture
`source.display_title` strings. The catalogue is not part of
`preview.markdown`, copy output, saved report create/revise/finalize
payloads, `provenance`, `source_digest`, or `reports.json`.

`source_digest` is `capture.canonical_digest({"date": <requested date>,
"day": projection["day"]})` over the same bounded single-day projection
already used for the preview. It is computed only after the core accepts
that projection. It never includes `generated_at`, weekly provenance,
runtime, or the client query. Same day facts at a different server clock
yield the same digest; a changed check-in, work fact, or title changes it.
Workspace identity stays the sibling `workspace_uid` field, not the digest.
Error responses do not include `source_digest`.

| Status | `error.code` | When |
| --- | --- | --- |
| 400 | `invalid_query` | Missing/blank/duplicate/unknown keys, non-canonical date or UUID, unsupported template. Stable message. No raw query text, no traceback, no `details` payload. |
| 409 | `workspace_mismatch` | Canonical UUID does not match the owning Store. Checked before the report read. |
| 409 | `store_sync_required` | Owning Store is not `in-sync`. Same envelope as other owner routes (`state`, `generation`, `changed_files`). |
| 400 | `invalid_host` | Inherited Host boundary. |
| 422 | `report_preview_unavailable` | Core bounded refusal of an otherwise valid projection (oversize/malformed after `review_projection`). Stable message. No raw data and empty `details`. |

This packet does not persist a draft. A later packet owns draft/UI follow-up.

## Out of scope

Product CLI wiring, draft persistence, AI generation, scheduling, an
editor, and finalize/publish all belong to later owners.
