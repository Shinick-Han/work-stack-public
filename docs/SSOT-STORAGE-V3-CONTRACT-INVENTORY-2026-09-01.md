# Work Stack SSOT v3 contract inventory

Date: 2026-09-01  
Status: frozen characterization input for the v4 normalization project  
Scope: the nine authoritative v3 JSON documents and the semantic information that a v4 reader or
migration must preserve

## 1. Purpose and interpretation

This inventory records the format that Work Stack writes today. It is not a claim that every field
is already enforced by one complete schema. The current contract has three different strengths:

1. `Store` strictly validates the document roster, workspace and Task identities, Store metadata,
   top-level auxiliary shapes, and planning-status fact chains.
2. `WorkStack` validates most records when it creates or consumes them.
3. Some nested auxiliary records are accepted by `Store` as opaque objects and are characterized
   here from the production writers and readers.

The distinction is important. A v4 validator may become stricter only after migration fixtures
show that existing valid stores remain semantically equivalent. It must not mistake a convention
for an already enforced invariant.

Executable fixtures live in:

- `tests/fixtures/store-v3/empty/`
- `tests/fixtures/store-v3/populated/`

`tests/test_store_v3_contract_inventory.py` freezes the complete field inventory, validates both
fixtures through `Store.initialize()`, verifies initialization does not change any authoritative
bytes, and freezes the populated semantic snapshot at:

```text
sha256:cca698f3d4137f0f4220eaa22102c6625a1e0de439cea364e9c0bcca0f15b36f
```

## 2. Authoritative roster

All nine documents are required once any authoritative document exists. Unknown sibling files are
not part of this v3 roster. Runtime locks, the journal, authority manifest, secrets, server state,
and synchronization receipts are not domain documents.

| Document | Version | Required root fields | Primary owner |
| --- | ---: | --- | --- |
| `workspace.json` | 2 | `version`, `id`, `name` | workspace identity |
| `backlog.json` | 3 | `version`, `tasks` | Tasks and embedded Task children |
| `store-meta.json` | 2 | `version`, `store_schema_version`, `migrations` | format/migration evidence |
| `okr.json` | 1 | `version`, `objectives` | objectives and embedded key results |
| `worklog.json` | 1 | `version`, `days` | review entries and work sessions |
| `notes.json` | 1 | `version`, `notes` | standalone graph notes |
| `captures.json` | 1 | `version`, `captures` | sanitized source captures |
| `replies.json` | 1 | `version`, `replies` | approved reply commands and receipts |
| `activity.json` | 2 | `version`, `activity`, `idempotency`, `planning_status` | three distinct event/ledger lifecycles |

## 3. Identity and migration metadata

### `workspace.json`

| Field | Shape and meaning |
| --- | --- |
| `version` | exactly integer `2` |
| `id` | non-nil, lowercase canonical RFC 4122 UUID; stable workspace identity |
| `name` | non-empty string; presentation label, not identity |

The workspace UUID shares the duplicate-UUID namespace checked for persisted Task UIDs.

### `store-meta.json`

`store_schema_version` is exactly `3`. `migrations` contains exactly `identity` and
`planning_status`; each evidence record contains exactly `id`, `origin`, and `source_sha256`.

Identity evidence variants:

- fresh: `id=workstack.store.v2`, `origin=fresh`, `source_sha256=null`;
- migrated v1: `id=workstack.store.v1-to-v2`, `origin=migrated_v1`, canonical SHA-256 source digest.

Planning evidence always uses `id=workstack.planning-status.v1`. Its origin is `fresh`,
`migrated_v1`, or `migrated_v2`; migrated variants require a canonical SHA-256 source digest.
Unknown metadata fields fail closed. A higher Store schema version is reported as newer than the
running build rather than guessed.

## 4. Task aggregate: `backlog.json`

`tasks` is an array. `Store` currently enforces the Task identity subset, while the application
owns the remaining writer/reader shape.

### Task record

| Field | Required in ready v3 | Meaning |
| --- | --- | --- |
| `id` | yes | unique display ID matching `T-[0-9]{4,}` |
| `uid` | yes | unique canonical UUID; authoritative identity |
| `revision` | yes | integer `0..9007199254740991` |
| `status_fact_id` | yes | current planning fact head, `PS-[0-9]{6,}` |
| `title` | writer-required | non-empty title |
| `detail` | writer-required | execution detail, possibly empty |
| `status` | writer-required | original/baseline status; current status is fact-projected |
| `priority` | writer-required | `P0`, `P1`, `P2`, or `P3` |
| `due` | writer-required | `YYYY-MM-DD` or null |
| `scheduled` | optional legacy field | `YYYY-MM-DD` or null |
| `estimate_minutes` | optional legacy field | null or integer `1..1440` |
| `tags` | writer-required | normalized string array |
| `objective_ids` | writer-required | display-ID references to objectives |
| `parent_id` | writer-required | null or Task display-ID reference |
| `dependencies` | writer-required | Task display-ID references |
| `subtasks` | writer-required | embedded subtask records |
| `notes` | writer-required | embedded Task-note records |
| `created` | writer-required | local calendar date |
| `updated_at` | writer-required | local calendar date |

Current planning status must be calculated from `activity.json.planning_status`; it must not be
read from `task.status`. `status_fact_id` must equal that Task's projected fact head. Revisions can
advance for non-status mutations between two planning facts, which is why a transition's
`prior_revision` may be greater than its predecessor's `new_revision`.

Subtask fields are `id`, `title`, `priority`, and `status`. The ID is scoped to the parent Task.
Embedded Task-note fields are `date` and `text`. Neither child currently owns an independent UID or
revision.

## 5. Objectives: `okr.json`

Objective writer fields are:

- `id`: display ID such as `O-1`;
- `quarter`: quarter label such as `2026-Q3`;
- `objective`: objective text;
- `status`: `active`, `done`, or `dropped`;
- `key_results`: embedded key-result array;
- `created`, `updated_at`: local calendar dates;
- `revision`: present on v1 API-created or subsequently revised objectives, but absent from older
  legacy-created objectives until a revision-aware mutation occurs.

Key-result fields are `id`, `text`, `target`, `progress`, and `status`. A key-result ID is scoped to
its objective. The parent Objective owns key-result revision and lifecycle today.

## 6. Review and work sessions: `worklog.json`

`days` maps ISO calendar dates to day objects. Day fields are independently optional as the day is
built by different operations:

- `start_time`: `HH:MM` check-in time;
- `entries`: worklog entry array;
- `sessions`: work-session array.

Entry base fields are `task_id`, denormalized `task` title, and the string arrays `done`, `next`,
and `blockers`. Entries produced from a completed work session additionally contain `session_id`
and non-negative `duration_seconds`.

Work-session fields are:

- `id`, matching `WS-[0-9]{6,}` and unique across days;
- `task_id` and denormalized `task` title;
- `date`, which must equal the containing day key;
- `state`: `running`, `paused`, or `stopped`;
- `started_at` and `updated_at`: UTC timestamps at whole-second precision;
- `segments`: one or more `{started_at, ended_at}` records;
- `worklog_state`: `not_ready` while active, then `pending` or `recorded` when stopped.

At most one session may be running or paused. A running session has exactly one open final segment;
paused and stopped sessions have none. Segments may not overlap or have negative duration.

## 7. Standalone notes: `notes.json`

A note contains `id`, `text`, `links`, and `created`. `links` contains normalized display IDs and
may include dangling references; the graph projection deliberately drops edges whose endpoints do
not exist. This differs from Task notes, which are embedded execution annotations.

## 8. Captures: `captures.json`

Stored capture fields are:

- validated packet fields: `schema_version`, `source_key`, `source`, `normalized`, `task_hints`,
  and `provenance`;
- Store-owned fields: `id`, `status`, `linked_task_ids`, `converted_task_ids`, `revision`,
  `created_at`, `updated_at`, and `recent_revisions`.

`status` is `inbox`, `linked`, `converted`, or `dismissed`. Linked and converted Task references
currently use display IDs. `recent_revisions` retains at most ten prior source revision summaries.

### Source

Source fields are `provider`, `resource_type`, `connection_ref`, `container_ref`, `object_ref`,
`version_ref`, `display_title`, `web_url`, `retrieved_at`, and `fingerprint`. References are bounded
opaque locators. Microsoft URLs must be token-free HTTPS URLs on allowlisted hosts. The fingerprint
and `source_key` are canonical digests of their contract-owned inputs.

### Normalized content

Fields are `summary`, `context`, `action_items`, and `tags`. Action fields are `id`, `title`,
`detail`, `priority`, and `due`; `task_id` appears after an action is converted. Raw message bodies,
recipients, headers, attachments, credential material, and hidden page content are not canonical
capture fields.

### Provenance and recent revisions

Provenance fields are `capture_mode`, `adapter`, `adapter_version`, `model`, `prompt_version`,
`redaction_policy_version`, `tool_trace_digest`, `allowed_tools`, `raw_retained`, and `created_at`.
Recent revision fields are `fingerprint`, `version_ref`, `retrieved_at`, `provenance_digest`, and
`redaction_policy_version`.

## 9. Replies: `replies.json`

A reply command contains:

- identity/links: `id`, `task_id`, `capture_id`, `capture_revision`;
- delivery contract: `provider`, `capability`, and `target`;
- approved content: `body`, `body_digest`, and `target_digest`;
- lifecycle: `state`, `approved_at`, `receipt`, `created_at`, and `updated_at`.

Targets contain `resource_type`, `connection_ref`, `container_ref`, `object_ref`, and
`version_ref`. States are `approved`, `sent`, `failed`, or `unknown`. The command body is stored only
after explicit approval.

A terminal receipt requires `schema_version`, `reply_id`, `provider`, `outcome`, `occurred_at`,
`body_digest`, and `target_digest`. It may contain `remote_message_ref`, `web_url`, or `error_code`.
The receipt's identity, provider, and digests must match the approved command.

## 10. Mixed ledgers: `activity.json`

### User-visible activity

Every event contains `id`, `type`, `created_at`, and `details`. Depending on the event type it may
also contain `capture_id`, `task_id`, or `reply_id`. IDs use the global document-local sequence
`E-[0-9]{6,}`. `details` is event-type-specific and is not currently centrally schema-validated.

### Request idempotency

Every record contains `key`, `method`, `path`, `request_digest`, `response_status`, and
`created_at`. It then has exactly one logical response form:

- `response_body`, containing the frozen response; or
- `response_ref={kind: reply, id: R-...}`, optionally with `response_meta`.

The second form prevents an approved reply body from being duplicated inside the idempotency
ledger. This ledger is operational deduplication state rather than immutable domain history.

### Planning-status facts

Facts have exactly these fields:

```text
id, type, task_id, task_uid, previous_fact_id, prior_revision, new_revision,
prior_status, status, created_at, actor, provenance
```

The type is `task.planning_status`. IDs are globally ordered `PS-[0-9]{6,}` values. Each Task starts
with a bootstrap fact whose predecessor/prior fields are null. Later facts form a per-Task chain,
advance revision by exactly one from `prior_revision`, change status, and use an allowlisted
actor/provenance pair. The fact head must match the Task's `status_fact_id`; a fact may not exceed
the Task's current revision.

## 11. Semantic snapshot boundary

The frozen v3 semantic snapshot deliberately removes physical collection wrappers while retaining
all authoritative meanings:

- workspace identity and label;
- complete Task records plus the independently projected current planning status;
- objectives, worklog days, standalone notes, captures, and replies;
- activity events, idempotency records, and planning facts as separate logical collections.

It excludes `store-meta.json` migration evidence and all runtime files. Migration evidence belongs
in a v4 migration receipt, while runtime manifests, locks, journals, and projections remain
rebuildable machine state. A v4 fixture is semantically equivalent only when it produces this same
logical snapshot, not merely the same counts or display IDs.

## 12. Write-surface characterization

At this inventory date there are 32 production call expressions that reach `save_many` directly,
including Store initialization/migration/seeding, maintenance restore, and domain writes. Domain
writes cover:

- Task create, note, subtask, status, scalar/relationship patch, and capture conversion;
- objective and key-result create/update;
- review check-in, review entry, work-session start/transition/record;
- standalone note create;
- capture ingest, link, dismiss, and conversion;
- reply approval and terminal receipt;
- idempotent replay bookkeeping.

Legacy convenience methods also call `Store.save()`, which delegates to `save_many`. This inventory
therefore freezes semantics rather than treating the current number of physical call sites as a
public contract. Wave 5 should remove physical filenames from service code slice by slice while
running one repository contract suite against both formats.

## 13. Known v3 normalization gaps

These are observed constraints for migration design, not permission to change v3 in place:

- display IDs still own several cross-document references;
- Task status has a baseline field and a separate fact-owned current value;
- Objective revisions are not present on every legacy record;
- Task notes and standalone notes overlap conceptually but have different schemas;
- `activity.json` combines audit history, request deduplication, and authoritative planning facts;
- auxiliary nested records do not all receive deep validation during Store startup;
- collection-level rewrites enlarge conflict scope even for one-record changes;
- timestamps mix local dates, local times, and UTC instants by domain purpose.

The v4 contract must model these differences explicitly. It must not normalize them by silently
discarding history, inventing identities, or changing public projections.
