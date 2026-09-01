# Work Stack v3 to v4 semantic mapping

Status: contract audit; migration code is not yet enabled  
Source: `docs/SSOT-STORAGE-V3-CONTRACT-INVENTORY-2026-09-01.md`

This mapping prevents normalization from silently deleting authoritative v3
meaning. Physical wrappers and field names may change, but a v4 repository must
be able to reconstruct the frozen v3 semantic snapshot.

## Deterministic identities

Existing Task and workspace UUIDs are retained. A v3 entity without a UID receives
a deterministic UUIDv5, never a random migration-time identity:

| v3 entity | UUIDv5 namespace and name |
| --- | --- |
| objective | workspace UID, `objective:<objective-id>` |
| key result | objective UID, `key-result:<key-result-id>` |
| capture | workspace UID, `capture:<capture-id>` |
| reply | workspace UID, `reply:<reply-id>` |
| standalone note | workspace UID, `note:<note-id>` |
| subtask | Task UID, `subtask:<subtask-id>` |
| embedded Task note | Task UID, `task-note:<zero-based-source-index>` |
| planning fact event | workspace UID, `planning-status:<fact-id>` |
| activity event | workspace UID, `activity:<event-id>` |
| worklog check-in | workspace UID, `worklog:<date>:check-in` |
| worklog entry | workspace UID, `worklog:<date>:entry:<source-index>` |
| work session | workspace UID, `worklog:<date>:session:<session-id>` |

The migration receipt must record the algorithm version and generated ID roster.
Display IDs remain in records or legacy event fields so the v3 public projection
can be reconstructed without parsing filenames.

`migration-receipt.schema.json` records deterministic mappings without copying
legacy display IDs or domain text into the receipt. Each general mapping stores
an entity kind, the digest of its canonical source identity, and generated UID.
Embedded Task notes additionally record Task UID, zero-based source index, and
generated note UID so duplicate note text remains distinguishable.

## Workspace and Store metadata

- `workspace.id` becomes both `workspace_uid` and the workspace record `uid`;
  `workspace.name` becomes `name` and its label remains unchanged.
- Workspace envelope dates are null for migrated v3 data because v3 records no
  workspace creation instant. `store.created_at` identifies creation of the v4
  candidate package, not creation of the v3 source workspace; diagnostics and
  projections must not present it as source history.
- v3 document versions are replaced by explicit v4 `format` and
  `schema_version` values.
- `store-meta.json` identity and planning migration evidence is not copied into a
  domain record. It must be preserved in the verified v4 migration receipt.
  The receipt preserves each exact `id`, `origin`, and nullable/source digest
  combination accepted by v3.

## Tasks and embedded children

| v3 Task meaning | v4 representation |
| --- | --- |
| `id`, `uid`, `revision` | `display_id`, unchanged `uid`, `revision` |
| title, detail, priority, due, scheduled, estimate, tags | same semantic fields; optional legacy schedule/estimate remain absent when absent |
| objective, parent, dependency display IDs | resolved stable UID fields; target records retain display IDs for reverse projection |
| `status` baseline | first planning-status event status; never duplicated in the Task record |
| `status_fact_id` | head planning event's `legacy_fact_id` |
| `created`, `updated_at` calendar dates | envelope dates, preserved without inventing a timezone or time of day |

Subtasks remain embedded. Their scoped v3 ID becomes `display_id`, a deterministic
UID is added, and title, priority, and **status** are preserved. Subtask status is
not projected from the Task planning stream.

Every embedded Task note becomes an independent `workstack.note` record with
`note_kind=task_annotation`, `task_uid` set, empty links, the original text, and
the original calendar date in both initial envelope date fields. Source order is
encoded in the deterministic UID mapping and migration roster; duplicate note
text is not collapsed.

## Objectives and key results

- v3 `objective` maps to v4 `title`; optional `description` remains absent. The repository's
  v3-compatible projection maps `title` back to `objective`.
- Objective and key-result statuses retain the v3 values `active`, `done`, and
  `dropped`; migration does not translate them into new vocabulary.
- Key-result `id`, `text`, `target`, integer percentage `progress` (`0..100`), and
  status remain embedded under the owning objective.
- Existing revision is retained with `revision_origin=explicit`. A legacy
  objective with no revision receives operational revision `0` plus
  `revision_origin=legacy_missing`, allowing a reverse semantic projection to
  omit rather than fabricate the original field.
- Calendar dates remain dates in the record envelope.

## Review days and work sessions

Each v3 day is expanded deterministically into worklog stream events:

1. `start_time`, when present, becomes a `check-in` event containing the exact
   day and local `HH:MM`; no UTC instant is invented.
2. Each `entries` item becomes an `entry` event retaining Task UID/display ID,
   denormalized Task title, `done`, `next`, `blockers`, and the optional
   `session_id` and `duration_seconds` fields. Missing v3 fields remain absent;
   they are not normalized to fabricated null values.
3. Each `sessions` item becomes one `session` event retaining session ID, Task
   identity/title, containing day, `running|paused|stopped` state, timestamps,
   every ordered segment including a nullable open `ended_at`, and
   `not_ready|pending|recorded` worklog state.

The cross-record validator must enforce the existing one-active-session rule,
day equality, segment ordering/non-overlap, and state/open-segment invariants.
Schema examples cover all three event variants; no session is flattened into a
duration-only summary.

## Standalone notes

Standalone notes become note records with `note_kind=standalone` and null
`task_uid`. The v3 `id`, text, and calendar date remain visible. `links` retain
their exact normalized display-ID strings, including dangling references. They
are deliberately not constrained to URLs or eagerly rewritten as UIDs because
the v3 graph projection ignores unknown endpoints rather than rejecting them.

## Captures

- Capture ID becomes `display_id`; the packet `schema_version` becomes
  `capture_packet_schema_version`; deterministic UID, revision, timestamps,
  source key, source, status, and provenance are retained.
- `normalized.summary`, `context`, **all action items**, and tags are retained.
  Each action preserves ID/title/detail/priority/due and retains its Task display
  ID while adding a resolved Task UID when available.
- `task_hints` remains an exact bounded string array. It is not forced into UID
  references because v3 permits hints rather than authoritative links.
- linked and converted known Task IDs become UID arrays.
- all up-to-ten `recent_revisions` retain fingerprint, version reference,
  retrieval time, provenance digest, and redaction policy version.

## Replies

Reply, Task, and capture display IDs map to deterministic/stable UIDs while the
reply display ID remains present. `capture_revision`, provider/capability, target
locators, approved body and digests, lifecycle state, approval time, and record
timestamps are retained.

A terminal receipt retains `schema_version`, reply display ID, provider, outcome,
`occurred_at`, body/target digests, and optional remote message reference, web URL,
or error code. Cross-record validation must verify receipt identity/provider/
digests against the command and require receipt outcome to agree with terminal
state. Target display title is optional because it did not exist in the v3 target
contract.

## Activity, planning status, and idempotency

Activity events retain the exact legacy `E-...` ID, event type, timestamp, opaque
JSON `details`, and resolved optional capture/Task/reply UIDs. A deterministic
event UID and migration provenance are added. Actor remains null when v3 did not
record one; migration does not guess a user. `details` remains recursively opaque
but is limited to the exact `workstack.canonical-json.v1` value space: null,
boolean, string, safe integer, array, and string-keyed object. Floats and integers
outside `-(2^53-1)..2^53-1` fail migration validation rather than producing an
event whose canonical digest cannot be calculated.

Planning facts retain the exact legacy and predecessor `PS-...` IDs in addition
to deterministic event UIDs. Task UID/display ID, revisions, prior/current status,
timestamp, actor, and provenance are unchanged. The validator must enforce chain,
head, transition, and Task-revision invariants. The Task's baseline and current
status are reconstructed from the first and head events respectively.

The request-idempotency ledger is operational runtime state rather than a
canonical append-only domain stream, but migration cannot drop it while replay
semantics may still depend on it. Its exact two response forms (`response_body` or
`response_ref` plus optional `response_meta`) must be copied into a bounded v4
runtime ledger before candidate activation. `idempotency-ledger.schema.json`
preserves key, method, path, request digest, response status, creation time, and
one replay form. A v3 reply display-ID reference is resolved to the v4 reply UID
in `response_ref.record_uid`; response bodies remain bounded canonical JSON and
may contain only the API `data` value plus the known replay metadata flags.

Every migrated record receives an explicit `expires_at`. The ledger declares a
bounded `retention_days` and `max_records` policy. Compaction may remove only
expired records; migration must fail closed when unexpired records exceed the
declared bound or duplicate keys disagree. Request bodies, headers, credentials,
and raw source payloads are never copied into this runtime artifact.

## Wave 3 activation prerequisites

The two previously deferred representational contracts now exist:

1. `migration-receipt.schema.json` preserves v3 migration evidence, generated UID
   rosters, source/candidate digests, backup/candidate artifact state, and all
   mandatory verification outcomes without domain content or paths;
2. `idempotency-ledger.schema.json` preserves bounded, unexpired replay records
   outside the activity stream with an explicit expiry/compaction policy.

Both schemas, their conversion fixtures, cross-record checks, source-byte digest,
and v3/v4 semantic parity must validate before candidate activation. No
implementation may interpret “outside canonical SSOT” as permission to silently
discard either artifact.
