# Checkpoint facts v1

Schema identity `workstack.checkpoint-facts.v1`.

One saved Task's LATEST ACTIVE recorded checkpoint, projected as facts. This is
the same behaviour the Task drawer already shows; the GUI selector
`frontend/src/features/tasks/taskResumeFacts.ts` and its summarizer
`frontend/src/domain/checkpointEntrySummary.ts` are the oracle, and
`workstack/checkpoint_facts.py` is the second implementation of it. Neither is
allowed to drift: `contracts/checkpoint-facts-v1/cases.json` holds hand-written
expected documents that both read.

This contract covers the pure core only. It is additive to the worklog and
changes nothing about the frozen Agent CLI contract, the recent-worklog context
brief, or any GUI behaviour.

## Public interfaces

```python
validate_checkpoint_request(*, workspace_uid: object, task_id: object) -> None
project_checkpoint_facts(audit: object, *, workspace_uid: str, task_id: str) -> dict[str, object]
render_checkpoint_facts(facts: dict[str, object], *, format: str = "json") -> str
```

All three are importable from `workstack.checkpoint_facts`. They are pure: no
file, no clock, no network, no Store, no Task lookup, no second audit read and
no write. `workstack/checkpoint_facts_format.py` holds the text builders and is
equally pure.

`validate_checkpoint_request` accepts only a canonical lowercase hyphenated
non-nil RFC 4122 workspace UUID — the spelling ordinary admission already
requires — and an ASCII Task ID matching `T-[0-9]{4,}`. It proves request
SYNTAX and nothing about existence, ownership or admission.

`project_checkpoint_facts` takes exactly one audit snapshot, as returned by
`WorkStack.list_checkpoint_audit()` or by the existing
`GET /api/v1/review/checkpoints` projection. It validates the request first,
then the snapshot, then projects.

`render_checkpoint_facts` returns the complete document or raises. `format` is
`json` or `markdown`.

## The facts document

Exactly these top-level keys, and no envelope, no `meta.command` and no claim of
`workstack.cli.v1`:

`contract`, `status`, `workspace_uid`, `task_id`, `provenance`, `done`, `next`,
`blockers`, `reasons`, `active_record_count`, `superseded_record_count`,
`version`.

`provenance` is `null`, or exactly `checkpoint_id`, `entry_digest`, `date`,
`ordinal`, `revision`, `origin`, `binding`, `recorded_task_id`,
`recorded_task_title`. These are the GUI's `TaskResumeProvenance` without its
redundant workspace and Task fields, which are already top level. Every one of
them is stored, never derived.

`status` is one of the original GUI facts statuses: `empty`, `unreadable`,
`partial`, `ready`. The progress adapter's renamed `none`/`unavailable`
spellings are not used. `loading` and `error` are lifetime states of a QUERY and
cannot occur in a pure projection over a snapshot the caller already holds.

`version` is byte-for-byte the GUI's own `selectTaskResumeFacts(...).version`:
`v1:<status>:<workspace_uid>:<task_id>`, and when a record was selected,
`:<checkpoint_id or legacy@<date>#<ordinal>>:<entry_digest or no-digest>:r<revision>`.

`active_record_count` and `superseded_record_count` are the GUI's matching
records for this workspace and Task, the selected one included. Superseded rows
stay in history and are counted, and are never selected.

### Selection

- One latest ACTIVE record, ordered by recorded `date` then `ordinal`. ISO-8601
  days sort as text; checkpoint identifiers are never parsed for order.
- The locator binding wins. Only a legacy row with no Task locator falls back to
  the Task its own opaque payload claims, and that weaker `entry-payload`
  binding is declared rather than hidden.
- Rows for another workspace or another Task contribute nothing and are not
  counted.
- An unreadable latest is reported as `unreadable`. An older readable record is
  NEVER promoted into its place.
- `done`, `next` and `blockers` all come from the ONE selected record. They are
  never blended across records, and a newer record's silence is the author
  saying nothing is in the way.
- A syntactically valid Task with no matching active record is `empty`: null
  provenance, empty facts, empty reasons, and the GUI's empty `version`. It is
  never an invented not-found result.
- Text preservation, the legacy single-string form and the list-slot rules are
  the existing `checkpointEntrySummary` semantics unchanged. Nonblank strings
  are not stripped and history is not blended. Blankness follows the oracle's
  `String.prototype.trim`, which keeps U+0085 where Python's `str.strip()` would
  not.
- Provenance is preserved on `partial` and on `unreadable`.
- The original metadata's own status and summary determine the status. No
  stricter reinterpretation is applied.

### Reason codes

Emitted in this fixed order when they apply:

1. `unpresented_values` — the summary left values it could not render.
2. `recorded_task_mismatch` — a non-null recorded Task ID differs from the
   requested, locator-bound Task.
3. `no_readable_summary` — the selected summary is not readable.

A reason may explain partial information. It never alters the status or the
version. No unknown label or value is emitted as an extra: the unpresented
values are COUNTED and stay in the audit the caller already holds.

### Untrusted text

The allowlisted summary strings, the recorded Task title and the recorded origin
are stored, untrusted text. They may legitimately contain a URL or a path
someone typed, and preserving that text verbatim is NOT permission to fetch or
open it. There is no new sanitizer rewriting existing Done/Next text.

Nothing else is extracted: no additional source URLs, no raw audit, no
unknown-field values, no transition history, no tokens and no filesystem
metadata.

## Renderings

JSON is compact, sorted-key, UTF-8, with exactly one trailing LF.

Markdown is English, headed `# Resume checkpoint`, with `## Selected checkpoint`
naming the record's identity, binding and state, `## Recorded progress` holding
Done/Next/Blockers, and `## Notes` holding fixed safe descriptions of the reason
codes. Empty and unreadable notices state what was not read; neither asserts
freshness or attestation, and Recent worklog is never substituted for this view.
Every untrusted value is wrapped with the existing
`workstack.agent_context_brief.fence`, whose fence grows past any run of
backticks the value contains, so no recorded text can open a heading, a list or
a code block of its own.

Each rendering is at most 32768 UTF-8 bytes including the final LF. An oversize
document fails entirely, before any output: facts are never truncated, dropped
or summarized further to fit.

## Refusals

`CheckpointFactsError(ValueError)` carries a fixed safe message and a closed
`code`. No submitted value, payload fragment, position or path is ever
interpolated.

| code | raised for |
| --- | --- |
| `invalid_request` | a non-canonical workspace UUID or a malformed Task ID |
| `invalid_audit` | audit metadata that selection depends on is missing, mistyped or ambiguous |
| `invalid_facts` | an unknown output format, or a facts mapping that is not the frozen shape |
| `output_too_large` | the finished document exceeds 32768 UTF-8 bytes |

The audit's own envelope workspace binding is validated, and each row's locator
workspace is checked again rather than trusting the envelope. Unrelated extra
TOP-LEVEL audit fields are ignored and never serialized.

The opaque `entry` payload is deliberately NOT validated. A malformed payload is
a real recorded state and maps through the summary semantics above; it is never
itself a refusal. Raw input is JSON-decoded data, so non-JSON cyclic object edge
cases are not a boundary this module claims to police, and there is no
requirement here to reimplement the storage audit validator or to inspect the
original files.

## Conformance

`contracts/checkpoint-facts-v1/cases.json` is read by `tests/test_checkpoint_facts.py`
and by `frontend/src/features/tasks/checkpointFacts.conformance.test.ts`.
Neither generates it. The cases cover empty, ordinary latest, superseded latest,
every-record-superseded, an unreadable latest over an older readable record, a
legacy free-text row, an opaque payload, partial mixed slots with an
unknown-field canary, a mismatched recorded Task ID, legacy `entry-payload`
binding, a legacy row naming another Task, foreign workspace and Task rows, a
checkpoint-only revision, and fence/UTF-8 canaries.
