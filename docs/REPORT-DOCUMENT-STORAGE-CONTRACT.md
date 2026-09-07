# Report document storage contract (T-0016 authoritative half)

Status: implementation-ready contract, repaired after independent review. No production
code is changed by this document.
Base: `affc011c27e3a98a3eb2fd1fb8d994621110de4e`
Supersedes: the first draft at `d0d046b96ffb97d6b1fd6e340af75c4a692ce638`
Review answered: `WAVE9-REPORT-STORAGE-CONTRACT-REVIEW.ko.md` (verdict FIX, items 1-8)
Scope: authoritative, human-edited report documents as first-class Store data.

The Wave 8 half that already exists is frozen input here: `workstack/reporting.py`
renders `daily-v1`, `GET /api/v1/reports/daily-preview` returns
`{workspace_uid, source_digest, preview}`, and
`frontend/src/features/review/reportDraftStorage.ts` keeps an origin-local draft that is
explicitly not authoritative. This contract adds the authoritative side and nothing else:
no AI, no scheduler, no new dependency, no multi-project platform.

**What the review changed.** The first draft treated `DEFAULTS` as a single roster that
could simply grow. It cannot: `DEFAULTS` is simultaneously the *current* roster and the
*historical* v1/v2/v3 roster for initialization, migration, backup verification, the
migration source freeze, and the experimental v4 adapter. §3 freezes versioned rosters so
those consumers stop sharing one mutable constant, and §7 replaces the unimplementable
"take a verified backup through the shipped maintenance path" step with a version-aware
sequence. §5, §9 and §8 settle the revision/state, replay-receipt and admission-order
questions the first draft left ambiguous.

---

## 1. The decision this document freezes

Authoritative report documents are a **tenth Store document**, `reports.json`, admitted
through a released schema migration. They are not a sidecar file. A sidecar cannot claim
the Store's lease, journal, atomic replacement, manifest, external-change detection, or
verified backup, and advertising those guarantees without them would be false.

The consequence is deliberate and must be budgeted: the v3 Store enforces an exact
nine-file roster, so a tenth file is a schema change with migration, manifest, backup,
restore, sync, validator and v4-adapter work. That work is specified in §3 and §7.

---

## 2. Schema numbering: the next legacy version is 5, not 4

The number 4 is already bound to a **different** storage architecture:

| Evidence | Location |
|---|---|
| v4 marker document is `store.json` with `{"format": "workstack.ssot", "schema_version": 4}` | `workstack/agent_authority.py:78-90` |
| v4 runtime manifest header requires `schema_set == "workstack.ssot.v4"` and `store_schema_version == 4` | `workstack/storage/manifest_store.py::_validate_manifest_header`, `workstack/storage/manifest.py::_manifest_core` |
| An accepted ADR defines v4 as records + bounded NDJSON streams, and explicitly **rejects** "keep the nine collection files unchanged" as the long-term format | `docs/SSOT-STORAGE-V4-ADR.md` |
| A non-destructive v3-to-v4 migration already ships | `workstack/storage/migration.py` (`MIGRATION_ALGORITHM_VERSION = "workstack.v3-to-v4.v1"`), CLI `storage migration plan\|preview\|execute\|verify` |

Authority detection reads the number, not only the marker file
(`workstack/agent_authority.py:107-108`):

```python
if has_v4_marker or metadata_schema == 4:
    return 4
```

So a collection-layout store whose `store-meta.json` said `store_schema_version: 4` would
be classified as v4 and refused by the agent CLI as `capability_not_enabled` while its
files are plainly the collection layout. That is a real regression, not a style concern.

**Decision: `workstack/store.py:60::STORE_SCHEMA_VERSION` becomes `5`.** The number 4 stays
reserved for `workstack.ssot`. v5 means "collection layout, ten documents".

Alternative considered and rejected: keep 3 and add `reports.json` as an optional file.
Rejected because `store.py:337::_validate_store_manifest_files` asserts
`set(files) == set(DEFAULTS)` and `maintenance.py:231` validates an exact archive file
count; "optional" would mean two valid rosters at one version, which is exactly the silent
acceptance this contract exists to prevent.

---

## 3. Versioned rosters (review item 1 and 3)

### 3.1 The defect in the first draft

`workstack/store.py:114::DEFAULTS` is read today by several kinds of caller, and only one
of them wants "the roster of the version this build writes".

`store.py:2307-2315::_existing_store_values_locked`:

```python
required_legacy = set(DEFAULTS) - {"store-meta.json"}
if existing not in (set(DEFAULTS), required_legacy):
    missing = sorted(set(DEFAULTS) - existing)
    raise StoreCorruptError("required store roster is incomplete: {}".format(", ".join(missing)))
```

`initialize()` (`store.py:2352-2361`) computes `existing = {name for name in DEFAULTS if
self.path(name).exists()}`. Add `reports.json` to `DEFAULTS` and an existing, healthy v3
authority presents nine names, which is neither `set(DEFAULTS)` (ten) nor
`required_legacy` (nine, but a *different* nine: it drops `store-meta.json` and adds
`reports.json`). **Every existing v3 store is refused as an incomplete roster before
migration can run.** The same substitution breaks the older paths:
`store.py:576-582::_validate_v2_auxiliary_stores` and
`store.py:1929-1943::_migrate_v1_locked` both iterate `DEFAULTS` and index
`values[name]` / `legacy_values[name]`, so a v1 or v2 input raises `KeyError` on the
absent `reports.json`.

### 3.2 Frozen historical rosters

Add to `workstack/store.py`, next to `DEFAULTS`:

```python
V1_DOCUMENT_NAMES: Final[frozenset[str]] = frozenset({
    "workspace.json", "backlog.json", "okr.json", "worklog.json",
    "notes.json", "captures.json", "replies.json", "activity.json",
})  # v1 has no metadata document
V2_DOCUMENT_NAMES: Final[frozenset[str]] = V1_DOCUMENT_NAMES | {"store-meta.json"}
V3_DOCUMENT_NAMES: Final[frozenset[str]] = V2_DOCUMENT_NAMES
V5_DOCUMENT_NAMES: Final[frozenset[str]] = V3_DOCUMENT_NAMES | {"reports.json"}
```

These are **historical facts and are never edited again.** `V1`/`V2`/`V3` are a literal
snapshot of today's `DEFAULTS` keys, written out by name rather than derived from
`DEFAULTS`, precisely so that a future tenth or eleventh document cannot silently rewrite
what "a v3 store" meant. `DEFAULTS` keeps its current role, the default *payloads* this
build writes, and its key set equals `V5_DOCUMENT_NAMES` once the roster entry lands. A
module-level assertion `set(DEFAULTS) == V5_DOCUMENT_NAMES` keeps the two honest.

Adding a sixth version later means adding constant number six and repointing only the
"current" consumers. It must never mean editing `V3_DOCUMENT_NAMES`.

### 3.3 Which roster every consumer uses

| Consumer | Location | Roster after this change |
|---|---|---|
| `_initialize_fresh_locked` | `store.py:2292` | `DEFAULTS` (v5); operation id becomes `store-initialize-v5` |
| `_existing_store_values_locked` roster gate | `store.py:2307` | §3.4 version detection, not one comparison |
| `_existing_store_readiness_locked` | `store.py:2324` | existing branch logic, plus the v3-to-v5 branch |
| `_migrate_v1_locked` source/writes loops | `store.py:1938`, `1978` | `V1_DOCUMENT_NAMES` |
| `_validate_v2_auxiliary_stores` | `store.py:576` | `V2_DOCUMENT_NAMES` |
| `_validate_ready_auxiliary_stores` | `store.py:300` | `DEFAULTS` (v5), reaching the new deep validator (§10) |
| `_validate_store_manifest_files` | `store.py:337` | `DEFAULTS` (v5), automatic |
| `_validate_store_metadata` | `store.py:284` | `STORE_SCHEMA_VERSION == 5`, third evidence record (§7.4) |
| `maintenance.create_backup_download` | `maintenance.py:82-86` | version-aware (§7.2) |
| `maintenance._read_archive_members` | `maintenance.py:147-148` | version-aware member set (§7.5) |
| `maintenance._validate_backup_file_record` | `maintenance.py:215` | version-aware allowlist (§7.5) |
| `maintenance._verify_backup_file_manifest` | `maintenance.py:231` | version-aware count (§7.5) |
| `maintenance._has_store_files` | `maintenance.py:280` | `V5_DOCUMENT_NAMES` (occupancy check; must stay maximal) |
| `owner_authority` roster loop | `owner_authority.py:88` | `DEFAULTS` (v5), automatic |
| `agent_authority._detect_format` | `agent_authority.py:106-114` | v3 branch accepts `metadata_schema in (None, 3, 5)`; 4 stays `workstack.ssot` |
| `storage/migration_source.V3_SOURCE_FILES` | `migration_source.py:23` | `tuple(sorted(V3_DOCUMENT_NAMES))`; **must stop tracking `DEFAULTS`** |
| `storage/validation._detect_format` legacy markers | `validation.py:120` | `V3_DOCUMENT_NAMES - {"workspace.json"}` |
| `storage/validation._v3_source_digests` | `validation.py:134` | `V3_DOCUMENT_NAMES` |
| `storage/repository._markers` | `repository.py:131` | `V3_DOCUMENT_NAMES - {"workspace.json"}` |
| `storage/read_repository.read` | `read_repository.py:91` | §4 semantic boundary, not a raw roster loop |
| `storage/experimental_application` load/path gates | `experimental_application.py:134`, `:142` | `V3_DOCUMENT_NAMES` (§6) |
| `storage/experimental_application._emit_refresh_event` | `experimental_application.py:286` | `sorted(V3_DOCUMENT_NAMES)` (§6) |

The rule the table encodes: **a caller that asks "what did an older store look like?" gets
a frozen constant; only a caller that asks "what does this build write?" gets `DEFAULTS`.**

### 3.4 Initialization order

`initialize()` must detect the on-disk version *before* judging the roster, so a complete
older store is never mistaken for an incomplete current one.

1. `_recover_locked()` first, unchanged: a pending journal is replayed before anything is
   classified.
2. Collect `existing` over `V5_DOCUMENT_NAMES`, the maximal set, not over `DEFAULTS`.
3. Classify:
   - `existing == V5_DOCUMENT_NAMES` and metadata says 5 → ready-state validation.
   - `existing == V3_DOCUMENT_NAMES` and metadata is a valid v3 record → **v3-to-v5
     migration** (§7).
   - `existing == V3_DOCUMENT_NAMES` and the metadata/backlog/activity shape matches the v2
     signature already tested at `store.py:2336-2341` → v2 migration first, then v3-to-v5,
     in the same held transaction.
   - `existing == V1_DOCUMENT_NAMES` (no `store-meta.json`) with `workspace.version == 1`
     and `backlog.version == 1` → v1 migration, then v3-to-v5, same held transaction.
   - `existing` empty → fresh v5.
   - anything else → `StoreCorruptError("required store roster is incomplete: ...")`, with
     the missing names computed against the roster of the **detected** version, so the
     message names real absences rather than `reports.json` on every legacy store.
4. Each legacy validator runs against its own roster and its own metadata rules before the
   upgrade writes anything. A v1 input is validated as v1, not as a v5 store with six
   missing documents.

v1 and v2 inputs are **supported**, not refused: the existing step semantics are preserved
unchanged and the same held migration additionally materializes the default `reports.json`
and v5 metadata. The chained upgrade is one lease, and each stage is its own `save_many`
so the journal can replay any stage.

Migration evidence and operation ids stop saying `v3`:

| Path | Operation id |
|---|---|
| fresh | `store-initialize-v5` |
| v1 chain | `store-migrate-v1-v5-<digest16>` |
| v2 chain | `store-migrate-v2-v5-<digest16>` |
| v3 only | `store-migrate-v3-v5-<digest16>` |

`_migrate_v1_locked`'s current id `store-migrate-v1-v3-...` (`store.py:1985`) is a name,
not a schema field; it changes with the roster and the change is asserted by a test.

---

## 4. Semantic document boundary (review item 4)

Report operations are **domain operations**, and their authority is `WorkStack`. They do
not open filenames.

`workstack/storage/document_repository.py` is the released semantic boundary: an
eight-member `WorkspaceDocument` enum (`document_repository.py:15-24`) mapped to physical
names (`:26-35`), behind the `DocumentRepository` protocol (`:52-65`). This contract adds
one member and one mapping:

```python
class WorkspaceDocument(Enum):
    ...
    REPORTS = "reports"

_PHYSICAL_NAMES = {..., WorkspaceDocument.REPORTS: "reports.json"}
```

Consequences that must be honoured rather than discovered later:

- `StoreDocumentRepository.total_bytes()` (`document_repository.py:91-92`) sums
  `_PHYSICAL_NAMES.values()`, so it starts including `reports.json` the moment the member
  is added. The §11 byte budget is stated against `reports.json` alone; B1 must re-read
  every `total_bytes()` consumer and confirm its limit is still the limit it intended.
- `workstack/report_documents.py` takes a `DocumentRepository`, never a `Store` and never a
  filename. Only `store.py` and `maintenance.py` know the string `"reports.json"`.
- **`WorkStack` decides revision, state and replay.** The HTTP adapter parses and refuses;
  the client sends what it was given; the UI displays. No orchestration, agent, CLI or
  front-end path computes a revision, chooses a state transition, or judges whether a
  request is a replay.
- While the experimental v4 adapter cannot represent reports (§6), a
  `WorkspaceDocument.REPORTS` load through it must refuse **content-free** with its
  existing `V4_APPLICATION_DOCUMENT_UNKNOWN` (`experimental_application.py:135`), and the
  feature must be refused at the capability level with `report_capability_unavailable`
  rather than surfacing an adapter error to a user.

---

## 5. Data model, revision and state (review item 5)

### 5.1 Document

`reports.json` default, in the shape style of `store.py:114-130`:

```json
{"version": 1, "reports": [], "idempotency": []}
```

| Field | Rule |
|---|---|
| `uid` | immutable UUIDv4 identity, assigned at creation |
| `workspace_uid` | equals `workspace.json.id`; never crosses workspaces |
| `template` | `daily-v1` only (§8.4) |
| `period` | `{"kind": "day", "date": "YYYY-MM-DD"}` |
| `source_digest` | digest of the source **at creation**, never recomputed, never rewritten |
| `source_generated_at` | `generated_at` of the preview the document was created from |
| `state` | `draft` \| `finalized` \| `archived` |
| `revision` | monotonic integer, starts at 1, incremented by **every** successful mutation |
| `archived_from_state` | `draft` \| `finalized` while `state == "archived"`; `null` otherwise |
| `archived_at`, `archive_note` | set on archive, cleared to `null` on restore |
| `revisions` | bounded ordered content history, newest last (§5.2) |
| `created_at`, `updated_at` | RFC 3339 UTC, second precision, matching `reporting_http.py:196-201` |

Identity is `uid`. `(workspace_uid, template, period)` is a uniqueness constraint **for
non-archived documents only**: one active report per period per template, while archived
documents for the same period stay readable.

### 5.2 One CAS token, and a separate content counter

The first draft called `revision` a CAS token but only advanced it on revise. That leaves a
window in which a client holding `expected_revision: 3` can apply a mutation to a document
that has since been finalized or archived.

**Frozen rule: `revision` is the document-level CAS token and increments by exactly 1 on
every successful mutation** — create (to 1), revise, finalize, archive, restore. There is
no second CAS token and no per-field version.

Content history is counted separately:

```json
{"content_revision": 1, "document_revision": 1, "markdown": "...",
 "authored_at": "...", "note": null}
```

- `content_revision` counts authored bodies, starting at 1. Only create and revise append.
- `document_revision` records the document `revision` at the moment that body was written,
  so a reader can align a body with the state timeline without a second CAS token.
- `markdown` is the human-edited body. It is never derived from the current source at read
  time.
- `note` is an optional bounded reason string on revise. The **archive** note is
  `archive_note` on the document, not a content entry: an archive writes no body, so it
  cannot append to a history whose every entry requires `markdown`. This removes the first
  draft's conflict with the markdown-free archive wire shape.

### 5.3 Transition table

| From | Operation | To | `revision` | `revisions` | Other |
|---|---|---|---|---|---|
| — | create | `draft` | to 1 | append `content_revision: 1` | records `source_digest` |
| `draft` | revise | `draft` | +1 | append | |
| `finalized` | revise | **`draft`** | +1 | append | editing a finalized report reopens it, and the response says so |
| `draft` | finalize | `finalized` | +1 | unchanged | |
| `finalized` | finalize | — | — | — | `report_state_invalid` |
| `draft`\|`finalized` | archive | `archived` | +1 | unchanged | sets `archived_from_state`, `archived_at`, `archive_note` |
| `archived` | revise/finalize/archive | — | — | — | `report_state_invalid` |
| `archived` | restore | `archived_from_state` | +1 | unchanged | clears the three archive fields |
| `draft`\|`finalized` | restore | — | — | — | `report_state_invalid` |

`archive` is a state transition, never a delete, following the shipped
`active`/`superseded` precedent in `workstack/checkpoint_transition.py:30`.

**Restore collision.** Restoring cannot resurrect a duplicate. If a non-archived document
with the same `(workspace_uid, template, period)` exists at restore time, restore refuses
with `report_duplicate_period` and writes nothing. The remedy is in the message: archive
the newer document first, then restore. The check runs inside the same held transaction as
the write, so two concurrent restores cannot both pass.

### 5.4 History exhaustion

`revisions` holds at most 20 entries (§11). At 20, a further revise is **refused** with
`report_revision_limit` and the document is untouched; nothing is dropped and no body is
overwritten. Finalize, archive and restore are still allowed at 20, because they append no
content. This is the cap that is actually reached: `MAX_REVISION` (`store.py:61`,
9_007_199_254_740_991) is a numeric ceiling a single-user tool will never approach, and is
documented only as the integer bound on `revision`.

---

## 6. The v4 adapter and the forward path (review item 3)

### 6.1 What breaks if `DEFAULTS` alone grows

`ExperimentalV4StoreAdapter` (`experimental_application.py:59`) projects a v4 authority
into the legacy read model. Three concrete failures:

1. `load()` and `path()` gate on `name not in DEFAULTS`
   (`experimental_application.py:134`, `:142`), then index `self._documents[name]` and
   `self._document_sizes[name]`. Those dictionaries come from `snapshot.to_v3_documents()`
   (`experimental_application.py:261`), which builds exactly nine documents
   (`storage/semantic.py:59-90`). Widening `DEFAULTS` turns `load("reports.json")` into a
   `KeyError` instead of a stable refusal.
2. `_emit_refresh_event` publishes `"changed_files": sorted(DEFAULTS)`
   (`experimental_application.py:286`), so every v4 refresh event would claim a document
   the adapter cannot produce.
3. `to_v3_documents` hard-codes `"store_schema_version": 3` (`semantic.py:66-68`). It is a
   v3 projection by construction and must stay one.

There is also a silent-loss path in the other direction:
`semantic.py:169-188::snapshot_from_v3_documents` reads named keys only and ignores any
document it does not know. `read_repository.read()` (`read_repository.py:91`) loads the
whole roster and hands it to that function, so a v5 store's `reports.json` would be
**dropped without error** on the way into a snapshot.

### 6.2 The choice this contract takes

**Option A: v5 reports are a released-Store-only capability in this scope.**

- `V3_DOCUMENT_NAMES` freezes the v4 adapter's roster (§3.3), so the v4 canary keeps
  projecting exactly the nine documents it projects today, byte for byte.
- The existing `storage migration plan|preview|execute|verify` v3-to-v4 command stays
  reproducible: its source roster becomes
  `V3_SOURCE_FILES = tuple(sorted(V3_DOCUMENT_NAMES))` and its output is unchanged for any
  store that has not been upgraded to v5.
- The report feature is refused, content-free, when the active backend is the v4 adapter
  (§4), with `report_capability_unavailable`.
- **A v5 store has no conversion into v4 in this scope.** The first draft's claim that "the
  v3-to-v4 conversion gains one more source family" is wrong on version arithmetic: an
  authority holding reports is v5, so the needed artifact is a **v5-to-v4** contract, which
  this document explicitly defers and names.

Option B — deliver a v4 `workstack.report` record family, its semantic projection, adapter
support and a v5-to-v4 conversion in this same scope — is a legitimate choice and roughly
doubles B1. It is **not** taken here, and the deferral is recorded as owed work rather than
left as an accident.

### 6.3 Deferred, and named

`docs/REPORT-DOCUMENT-V5-TO-V4-CONVERSION.md` (not written by this contract) owes: the
`workstack.report` record family, whether `revisions` becomes a bounded NDJSON stream, the
report ledger's place in the v4 runtime ledger (`storage/migration_idempotency.py`, with
`MAX_LEDGER_RECORDS = 10_000` at `:18` and `RETENTION_DAYS = 30` at `:17`), and a
`MIGRATION_ALGORITHM_VERSION` of its own. Nothing in §5 depends on the collection layout —
identity is `uid`, references are UIDs, the ledger is bounded runtime metadata — so the
deferral is a scheduling decision, not a design dead end.

---

## 7. Migration: v3 nine documents to v5 ten documents (review item 2)

### 7.1 Why the first draft's step 3 was not implementable

The first draft said "create a verified backup through the shipped `maintenance` path".
`maintenance.create_backup_download` (`maintenance.py:82-86`) calls
`store.consistent_read()` and iterates `sorted(DEFAULTS)`. Run from a build whose
`DEFAULTS` is the v5 roster and whose `STORE_SCHEMA_VERSION` is 5, against a v3 authority:
`consistent_read()` requires a ready state, ready-state validation requires
`store_schema_version == 5` (`store.py:284-293`), and the roster loop demands a
`reports.json` that does not exist. The pre-migration backup cannot be taken by that
function as written. The same coupling holds for `_read_archive_members` (`:147`),
`_validate_backup_file_record` (`:215`), `_verify_backup_file_manifest` (`:231`) and
`_validate_backup_store` (`:238`), the last of which validates an archive by running
`Store(validation_root).initialize()`, i.e. the *new* initialize.

### 7.2 The frozen sequence

All of it inside one `Store.transaction()` (`store.py:1733`), which is depth-counted and
holds the writer lease:

1. **Recover.** `_recover_locked()` (`store.py:2251`) replays any pending journal first, so
   migration never starts on a half-written generation.
2. **Read as v3.** Read the nine documents with the v3 roster and validate them with the v3
   metadata rules — a version-parameterized validator, not the v5 ready-state validator.
   Refuse here, before any write, if the input is not a valid store of its detected version.
3. **Pre-backup, in the detected input format, from those frozen bytes.** A version-aware
   `create_backup_download(store, *, roster, schema_version)` (or an internal
   `_backup_bodies(bodies, workspace_id, schema_version)` seam) writes a nine-member
   archive whose manifest records `store_schema_version: 3`. It uses the bytes already read
   under the held lease, not a second read. The archive is verified under the **v3** rules
   before the migration proceeds; a verification failure aborts the migration with nothing
   written.
4. **Build the v5 candidate.** The nine payloads carry their existing semantics unchanged,
   plus the default `reports.json`, plus `store-meta.json` with `store_schema_version: 5`
   and a third migration evidence record (§7.4).
5. **Publish in one journalled commit.** A single
   `save_many(..., operation_id="store-migrate-v3-v5-<digest16>")` (`store.py:2195`) writes
   all ten values through the journal and atomic replacement.
6. **Manifest.** The ten-file manifest is written from the committed generation;
   `_validate_store_manifest_files` (`store.py:337`) now compares against the v5 roster.

For a v1 or v2 input the chain is 2 → (the legacy migration exactly as it exists today) →
3 → 4 → 5 → 6 under the same lease, with the pre-backup taken in the format of the
**detected input version**, so restoring that backup restores exactly what the user had.

### 7.3 Interruption

Three interruption points, each with a defined outcome, and each a required test (§16):

| Killed | On reopen |
|---|---|
| before the journal is written | the nine-file v3 generation stands; migration is simply retried |
| after the journal is written, some replaces done | `_recover_locked()` replays the journal to the complete ten-file generation |
| after the commit, before the manifest is published | the manifest is regenerated from the committed generation on the next `initialize()`, and sync is re-inspected |

There is no half-migrated state to hand-repair: either the journal completes the ten-file
generation or the nine-file generation stands.

**Honest limit.** The nine documents are semantically unchanged and their content is
preserved. `store-meta.json` and the runtime manifest necessarily change, and canonical
re-serialization may alter incidental formatting bytes of documents that were not already
canonical. This contract promises **semantic preservation plus content preservation for the
nine payloads**, and does not promise that every byte in the authority is unchanged.

### 7.4 Metadata evidence

`_validate_store_metadata` (`store.py:284`) currently requires
`set(migrations) == {"identity", "planning_status"}` via `_migration_evidence_records`
(`store.py:226-233`). v5 adds a third record:

```json
"reports": {"id": "workstack.reports.v3-to-v5", "origin": "migrated_v3", "source_sha256": "sha256:..."}
```

`origin` is `fresh` for a store initialized at v5, and `migrated_v1` / `migrated_v2` /
`migrated_v3` for an upgrade, matching the vocabulary the identity record already uses.
`source_sha256` is the compact-JSON digest of the pre-migration document set, computed the
way `_migrate_v1_locked` computes it (`store.py:1943`).

### 7.5 Backup, restore, export, sync at ten documents

- **New backups** contain ten payload members plus `BACKUP_MANIFEST`
  (`maintenance.py:22`), and the manifest records `store_schema_version: 5`.
- **Reading an archive is version-aware.** `_read_archive_members`,
  `_validate_backup_file_record` and `_verify_backup_file_manifest` take the roster from
  the archive manifest's `store_schema_version` instead of from `DEFAULTS`. The manifest is
  decoded and its header validated first (`_decode_backup_manifest`, `:167`;
  `_validate_backup_manifest_header`, `:185`), then the member set is checked against the
  roster for that version. An archive claiming a schema version **newer than this build**
  is refused before any member is read.
- **Restoring a nine-member v3 archive into a v5 build is accepted, and is a conversion,
  not a file drop.** In order: verify the archive under v3 rules; validate the projected
  store under v3 rules (`_validate_backup_store`, `:238`, parameterized by version rather
  than always calling the current `initialize()`); then write the destination as a v5
  authority — nine payloads, default `reports.json`, `store-meta.json` at 5 with a
  `migrated_v3` reports evidence record. Adding an empty file and letting the new
  `initialize()` sort it out would leave the destination's metadata claiming 3.
- **Restoring a ten-member v5 archive into an older build** is refused by §7.6.
- **Same-workspace only.** The archive's `workspace_id` must equal the destination
  authority's, as the shipped restore already requires. A report document carries
  `workspace_uid` inside it as a second, independently checkable binding, and every report
  in a restored `reports.json` must match it or the restore refuses.
- **Sync** is manifest-driven; the ten-file roster flows through
  `_validate_store_manifest_files` with no special case. An unowned edit to `reports.json`
  freezes mutations exactly like an unowned edit to any other document.

### 7.6 Old build meets a new store

Already implemented and reused unchanged (`store.py:290-293`):

```python
if schema_version != STORE_SCHEMA_VERSION:
    if type(schema_version) is int and schema_version > STORE_SCHEMA_VERSION:
        raise StoreCorruptError("store schema is newer than this Work Stack build")
    raise StoreCorruptError("store schema version is invalid")
```

An older build opening a v5 store fails closed and writes nothing;
`_validate_store_manifest_header` (`store.py:319`) refuses the manifest for the same
reason. The one required addition is that this reaches the user as guidance — "this
authority was upgraded by a newer Work Stack; update the application" — rather than only as
a corruption error in a log.

### 7.7 Rollback

The pre-migration verified backup from §7.2 step 3 is the rollback, and it is in the format
of the version the user actually had. Reverse conversion is not implemented, matching the
v4 ADR's stance. Rolling back loses report documents authored after the migration; the
migration receipt states this before it runs.

---

## 8. Transaction, admission and held reads (review item 7)

### 8.1 Admission order, and how the UID is read before the body

The shipped daily preview admits in the order UID, sync, work (`reporting_http.py:58-66`)
and takes the UID from a strict query parameter set (`reporting_http.py:26`:
`REQUIRED_QUERY_KEYS = ("date", "template", "workspace_uid")`). A create request has no
path UID, and its `workspace_uid` sits in the body, so "UID before body" needs a mechanism,
not just an ordering claim.

**Frozen: `workspace_uid` is a required query parameter on every report route, GET and POST
alike, exactly one value, canonical RFC 4122 form** (`_canonical_workspace_uid`,
`reporting_http.py:187`). Mutations still carry `workspace_uid` in the body; the two must
be equal, and a mismatch is `workspace_mismatch`. The body copy is a second, independently
checkable binding, not the admission input.

Order inside the held transaction, no step skippable:

1. **Query decode** — bounded, exact key set, single-valued. A malformed query is
   `invalid_query` with no store access.
2. **UID admission** — `readiness.workspace_uid == workspace_uid` or `workspace_mismatch`,
   content-free, mirroring `_require_admitted_owner` (`reporting_http.py:148`).
3. **Sync guard** — `store.sync_status()["state"] == "in-sync"` or `store_sync_required`
   with the shipped 409 shape (`reporting_http.py:161`).
4. **Idempotency key** — presence and pattern only (§9). No body semantics yet.
5. **Body admission** — size bound, then shape, then semantics (§10.1).
6. **Idempotency replay** — before CAS and before any source computation (§9.2).
7. **Revision CAS** — `expected_revision == document.revision` or
   `report_revision_conflict`, nothing written.
8. **State and collision** — §5.3, including the restore duplicate check.
9. **Source digest** — §8.3.
10. **Commit** — one `save_many({"reports.json": ...})` so the document mutation and its
    ledger entry land in a single journalled atomic replacement.

Steps 6 and 10 are inseparable: a ledger entry written outside the same commit as its
document change permits a duplicate on crash.

### 8.2 Held reads

Reads are not exempt. `GET /api/v1/reports` and `GET /api/v1/reports/{uid}` run inside
`Store.consistent_read()` (`store.py:1757`) and re-check the owner UID and sync **inside**
the held snapshot, then check sync again after the projection — the same before/after
pattern the shipped preview uses at `reporting_http.py:104` and `:109`. A list assembled
across two generations is never returned.

### 8.3 Stale calculation happens in the held snapshot

`source_stale` is computed inside the same held transaction that produced the document,
never from a second read:

```python
# workstack/reporting_http.py:88
def day_source_digest(*, date: str, day: Any) -> str:
    return canonical_digest({"date": date, "day": day})
```

`canonical_digest` is `workstack/capture.py:119` — sorted-key compact UTF-8 JSON, prefix
`sha256:`. The digest covers the **source day**, not the rendered preview, and does not
include `generated_at`. This contract reuses that function and defines no second digest.

| Operation | Current-source behaviour |
|---|---|
| create | computes the digest from `stack.review_projection(date, 1)` **in the same transaction** (the call the shipped preview makes at `reporting_http.py:107`) and stores it; the client's asserted `source_digest` must equal it, else `report_source_changed` |
| revise | compares and reports `source_stale: true` in the response; the edit is accepted, because the human is editing their own prose |
| finalize | same as revise: staleness is reported, never auto-refreshed |
| archive / restore | not computed; these do not touch content |
| GET read | computed in the held read snapshot and returned as `source_stale` |
| GET list | **not** computed per item; N documents would need N projections. The list omits `source_stale`, and that absence is part of the list shape |

No operation regenerates markdown, and no operation rewrites a stored `source_digest`.
There is no automatic re-render anywhere in this contract.

### 8.4 `daily-v1` only, until a weekly source digest exists

The first draft accepted `weekly-v1` "when that template constant exists". It does not
exist: `workstack/reporting.py:17` defines `TEMPLATE_DAILY_V1` and nothing else, the
`weekly` symbols at `reporting.py:170` are a projection *range* field, and the agent
contract records `"weekly_projection_forbidden": True`
(`workstack/agent_cli_contract.py:775`). More decisively, the only source-digest contract
that exists is `day_source_digest`, which is defined over a single day.

**Frozen: the first storage implementation accepts `template == "daily-v1"` only.** Any
other value is `report_template_unsupported` (422). Weekly is enabled only after a separate
canonical weekly source-digest contract defines what a week's source is and how its digest
is computed. `period.kind` is likewise `"day"` only; the `{"kind": "week", "start", "end"}`
shape is reserved by this sentence and validated by nothing yet.

---

## 9. Idempotency: an immutable, bounded, full receipt (review item 6)

### 9.1 Why `{uid, revision}` is not enough

The first draft stored `response_ref: {uid, revision}` and promised replay of the original
status and full document. It cannot deliver that. After a later finalize, archive, restore
or revise, the current document no longer carries the state, the revision view or the
`source_stale` flag the original response carried, and there is nothing to reconstruct them
from. The shipped `WorkStack._idempotency_replay` (`service.py:3789`) is not a precedent
either: its `response_ref` branch re-projects a *reply*, which is immutable, and its
general body branch returns `{"status": 200, ...}` regardless of the recorded
`response_status` (`service.py:3826`). "Status unchanged" is therefore a new promise, not
an inherited one.

### 9.2 The frozen ledger record

`reports.json.idempotency` is this feature's **own** ledger. It is not `activity.json`, and
that separation is what lets §5's byte-equality claim hold.

```json
{"key": "...",
 "method": "POST",
 "path": "/api/v1/reports",
 "request_digest": "sha256:...",
 "response_status": 201,
 "response_body": {"...the complete original response body..."},
 "created_at": "2026-09-06T12:00:00Z"}
```

- The record is **immutable once written.** No later mutation edits, re-points or
  re-projects it. This is the whole difference from `response_ref`.
- `response_body` is the full original response, stored verbatim, and is what replay
  returns — including the original `state`, `revision`, `content_revision` and
  `source_stale`. A replay after ten later mutations still returns the original answer.
- `response_status` is returned **as recorded**: `201` for a create replay, `200` for the
  others. The response carries `"replayed": true`.
- Key format reuses the shipped pattern `[A-Za-z0-9._:-]{8,128}`
  (`service.py:3781-3787::_validate_idempotency_key`).
- Same key + same `method`/`path`/`request_digest` returns the stored record. Same key +
  any difference is `idempotency_conflict` (409), nothing written.

**Bounding a full body.** A `daily-v1` markdown is capped at 100_000 characters and a
document keeps at most 20 of them, so a naive stored body could approach 2 MB. The record
is therefore bounded by construction: `response_body` stores the document **without the
`revisions` array**, plus the single content entry this operation wrote (absent for
finalize/archive/restore). One markdown, never twenty. Replaying a full history is a `GET`,
which is not an idempotent-write concern. `MAX_LEDGER_RECORD_BYTES` (§11) is checked before
the write, and a receipt that would exceed it refuses the **mutation** with
`report_body_invalid` rather than silently storing a truncated receipt.

**Replay position.** Replay runs inside the held transaction, after UID admission and the
sync guard, and **before** the CAS check and before any current-source computation. A
client that never saw the original response retries with the same key and the same body and
gets the original result back *even if the source day has changed since*, because replay
returns the recorded body and never re-evaluates the source.

### 9.3 Ledger exhaustion is a refusal, not a deletion

The first draft's "prune by age, then by count, capped at 1_000" is unsatisfiable: with
1_000 records all younger than the 30-day retention window there is nothing prunable and
the cap is already met.

Frozen rules:

1. On every mutation, first prune records whose `created_at` is older than
   `REPORT_LEDGER_RETENTION_DAYS` (30).
2. If the surviving count is still `REPORT_LEDGER_MAX_RECORDS` (1_000), the mutation is
   **refused** with `report_idempotency_capacity` (429), content-free. No fresh record is
   deleted to make room, because deleting a fresh receipt silently converts a replayable
   retry into a duplicate write.
3. The refusal states the honest remedy: retries are exhausted for the current window, and
   capacity returns as records age out. The response includes `retry_after_seconds` derived
   from the oldest surviving record's expiry.
4. This ceiling needs about 1_000 distinct mutations inside 30 days in a single-user tool,
   far above normal use. It exists so the failure mode is a stated refusal rather than a
   silently growing document.

The first draft's example wrote `"revision": "3"` as a string. `revision` is an **integer**
everywhere in this contract.

---

## 10. Deep validation and bounded listing (review item 8)

### 10.1 `reports.json` needs a real validator

`store.py:212-224::_validate_auxiliary_store` checks only the top-level key set, the
`version` value and container types. That is adequate for `notes.json`; it is not adequate
for a document holding identities, a state machine, ordered history and a replay ledger.

`reports.json` gets a dedicated deep validator, called from
`_validate_ready_auxiliary_stores` (`store.py:300`), refusing with a stable
`StoreCorruptError` on any of:

- top level not exactly `{version, reports, idempotency}` with `version == 1`;
- `uid` not a canonical RFC 4122 UUID, or repeated across documents;
- `workspace_uid` absent, non-canonical, or unequal to `workspace.json.id`;
- `template` not `daily-v1`; `period` not `{"kind": "day", "date": "YYYY-MM-DD"}` with a
  round-tripping ISO date;
- `state` outside `{draft, finalized, archived}`; `archived_from_state` present when not
  archived, or absent/invalid when archived;
- `revision` not a positive `int` (Python `bool` rejected explicitly via `type(x) is int`,
  the idiom already used at `store.py:291` and `maintenance.py:218`), or above
  `MAX_REVISION`;
- timestamps not RFC 3339 UTC with an explicit offset, or `updated_at < created_at`;
- `revisions` not a list, longer than 20, with non-contiguous, non-ascending or duplicated
  `content_revision`, or a `document_revision` exceeding the document's `revision`;
- `markdown` not a `str`, or longer than `MAX_MARKDOWN_CHARS` (`reporting.py:19`, 100_000),
  counted in code points to match `reporting.py:71`;
- more than one non-archived document per `(workspace_uid, template, period)`;
- a ledger record missing a required field, holding an unknown field, carrying a duplicate
  `key`, or failing the key pattern;
- `request_digest` or `source_digest` not matching `sha256:[0-9a-f]{64}`;
- any `float` that is NaN or infinite, any string containing a lone surrogate, or any
  nesting deeper than a fixed bound — the stored response body is data that must be
  depth-limited before it is re-serialized;
- total canonical UTF-8 serialization above `MAX_REPORTS_BYTES` (§11).

Every refusal is content-free: a code and a field name, never a body excerpt.

### 10.2 Listing is ordered, bounded and cursor-bound

- **Order is frozen: `(updated_at DESC, uid ASC)`.** `uid` breaks ties so the order is
  total and a cursor is unambiguous.
- **`state` is a filter alias, not a stored value.** `state=active` selects
  `draft|finalized`; `state=archived` selects `archived`; `state=all` selects everything.
  No document is ever stored with `state == "active"`.
- **Cursor** is an opaque token encoding `(updated_at, uid)` plus a digest of the filter and
  the `workspace_uid` it was issued for. A cursor presented with a different filter or a
  different workspace is refused with `report_cursor_invalid` (400), content-free. A cursor
  whose anchor document no longer exists is likewise refused rather than silently
  restarted, so a caller never pages past unseen items believing it saw them all.
- **Page size** is 50, and the envelope reports `omitted_count` for the remainder.
- Archived documents are never absent from the store; they are absent from the `active`
  filter and present under `archived` and `all`.

---

## 11. Limits

Each anchored to an existing constant so the product stays one size.

| Limit | Constant | Value | Anchor |
|---|---|---|---|
| markdown per content revision | `MAX_MARKDOWN_CHARS` | 100_000 chars | `reporting.py:19`, `reportDraftStorage.ts::REPORT_DRAFT_MAX_MARKDOWN` |
| content revisions per document | `MAX_REPORT_CONTENT_REVISIONS` | 20 | new; bounded history for a single-user tool |
| documents per workspace | `MAX_REPORT_DOCUMENTS` | 500 | new; about 18 months of daily reports |
| `reports.json` total bytes | `MAX_REPORTS_BYTES` | 8 MiB | well below `maintenance.py:23::MAX_BACKUP_BYTES` (128 MiB) |
| ledger records | `REPORT_LEDGER_MAX_RECORDS` | 1_000 | smaller sibling of `storage/migration_idempotency.py:18::MAX_LEDGER_RECORDS` (10_000) |
| ledger retention | `REPORT_LEDGER_RETENTION_DAYS` | 30 | `storage/migration_idempotency.py:17::RETENTION_DAYS` |
| one ledger record | `MAX_LEDGER_RECORD_BYTES` | 192 KiB | one 100_000-char markdown plus envelope, canonical UTF-8 |
| list page size | — | 50, with `omitted_count` | the bounded-list style used across the product |
| `revision` ceiling | `MAX_REVISION` | 9_007_199_254_740_991 | `store.py:61` |

### 11.1 Exhaustion: no silent deletion of authored data

- **Content-revision cap** → `report_revision_limit` (409). The document is untouched; old
  bodies are **not** dropped. Finalize, archive and restore still work (§5.4).
- **Document cap** → `report_document_limit` (409) on create. Nothing is evicted.
- **Byte cap** → `report_storage_full` (409). Nothing is evicted.
- **Ledger** → age-prune first, then refuse with `report_idempotency_capacity` (429). Fresh
  receipts are never deleted (§9.3).
- No path in this contract deletes a report document or a content revision. `archive` is
  the only removal the user gets, and it is reversible.

---

## 12. Errors

Failures carry a code, a safe message and no store content, following the shipped
`DailyPreviewHttpError` style (`reporting_http.py:148-176`).

| Code | Status | Meaning |
|---|---|---|
| `invalid_query` | 400 | query key set, arity or format is wrong |
| `workspace_mismatch` | 409 | query UID is not the admitted owner, or body UID differs from query UID |
| `store_sync_required` | 409 | authority is not in sync |
| `idempotency_key_required` | 400 | missing header on a mutation |
| `invalid_idempotency_key` | 400 | key fails `[A-Za-z0-9._:-]{8,128}` |
| `idempotency_conflict` | 409 | same key, different request |
| `report_idempotency_capacity` | 429 | ledger full of unexpired receipts (§9.3) |
| `report_revision_conflict` | 409 | `expected_revision` does not equal the stored `revision` |
| `report_source_changed` | 409 | create-time asserted digest does not equal the current source digest |
| `report_not_found` | 404 | unknown `uid` in this workspace |
| `report_state_invalid` | 409 | transition not allowed from the current state (§5.3) |
| `report_duplicate_period` | 409 | a non-archived document already covers this period/template — on create **and** on restore |
| `report_revision_limit` | 409 | §11.1 |
| `report_document_limit` | 409 | §11.1 |
| `report_storage_full` | 409 | §11.1 |
| `report_body_invalid` | 400 | bounds, fields or encoding violated; also an over-large ledger receipt |
| `report_cursor_invalid` | 400 | cursor is stale, or bound to a different filter or workspace |
| `report_template_unsupported` | 422 | template is not `daily-v1` (§8.4) |
| `report_capability_unavailable` | 422 | the active backend cannot hold reports (v4 adapter, §6.2) |

---

## 13. Wire shapes

All routes are loopback. Reads are GET. Every mutation is POST with `Origin`, CSRF and a
required `Idempotency-Key`, exactly as `workstack/server.py::_idempotency_key` already
enforces for other writers. `workspace_uid` is a required query parameter on **every**
route (§8.1).

```text
GET  /api/v1/reports?workspace_uid=...&state=active|archived|all&limit=50&cursor=...
GET  /api/v1/reports/{uid}?workspace_uid=...
POST /api/v1/reports?workspace_uid=...                      create   (draft)
POST /api/v1/reports/{uid}/revisions?workspace_uid=...      revise
POST /api/v1/reports/{uid}/finalize?workspace_uid=...       finalize
POST /api/v1/reports/{uid}/archive?workspace_uid=...        archive
POST /api/v1/reports/{uid}/restore?workspace_uid=...        restore
```

List item — no markdown, and no `source_stale` (§8.3):

```json
{"uid": "...", "template": "daily-v1", "period": {"kind": "day", "date": "2026-09-06"},
 "state": "finalized", "revision": 4, "content_revision": 2, "source_digest": "sha256:...",
 "archived_from_state": null, "created_at": "...", "updated_at": "..."}
```

List envelope: `{"workspace_uid": "...", "reports": [...], "omitted_count": 0, "cursor": null}`.

Read returns the item plus `revisions` (the full history) and `source_stale: true|false`.

Create request:

```json
{"workspace_uid": "...", "template": "daily-v1",
 "period": {"kind": "day", "date": "2026-09-06"},
 "source_digest": "sha256:...", "source_generated_at": "...", "markdown": "..."}
```

Revise:

```json
{"workspace_uid": "...", "expected_revision": 3, "markdown": "...", "note": null}
```

Finalize / restore:

```json
{"workspace_uid": "...", "expected_revision": 3}
```

Archive:

```json
{"workspace_uid": "...", "expected_revision": 3, "note": null}
```

`markdown` is required for create and revise and **forbidden** everywhere else; `note` is
accepted on revise and archive only. Every mutation response returns the document (without
`revisions`, per §9.2) plus `"replayed": true|false`, and a revise that reopened a
finalized document reports `"reopened": true`.

### 13.1 Markdown inertness

Stored markdown is **authored prose** and is stored verbatim; it is not passed through
`reporting.py::inert_text`, because escaping the user's own headings would corrupt what
they wrote. Inertness is a **rendering** obligation:

- the API never returns markdown as HTML;
- the UI renders it as text nodes, exactly as the shipped `reportDraftEditorViews.tsx`
  reading view does with `<pre>`;
- no `dangerouslySetInnerHTML`, no markdown-to-HTML dependency;
- `inert_text` remains the rule for *generated* facts inside `preview_daily_report`, which
  this contract does not change.

---

## 14. Client revision vs server revision

`reportDraftStorage.ts::localRevision` and the server `revision` are **different counters
in different stores** and must never be compared or displayed as one number. The document
now also carries `content_revision`, so the UI has three numbers and may show at most two.

- `localRevision` counts saves of the origin-local buffer on one device.
- `revision` counts every authoritative mutation, including state transitions.
- `content_revision` counts authored bodies, and is the only server number worth showing
  next to prose ("version 3 of this report").
- Promotion sends `markdown` and, for revise, the server `expected_revision`. It never
  sends `localRevision`.
- On successful promotion the local draft is **kept**, not deleted, and the UI shows two
  labelled facts ("local draft on this device" and "report version N").
- On any failure the local text is untouched, and Copy Markdown / Download .md stay
  available. This is the Wave 8 rule and it is inherited, not re-decided.

---

## 15. Ownership batches and order

Strictly serial where state shape is at stake; the client contract is one batch so two UI
owners do not both edit `client.ts`.

```text
B1 storage + migration ---> B2 HTTP ---> B3 client API ---> B4a promotion UI
                                                       \-> B4b list/archive UI
```

| Batch | Owned files | Notes |
|---|---|---|
| **B1** storage + migration | `workstack/report_documents.py` (new), `workstack/store.py` (frozen rosters, version 5, deep validator, metadata evidence, version-aware initialize), `workstack/maintenance.py` (version-aware backup/verify/restore), `workstack/storage/document_repository.py` (`REPORTS` member and mapping), `workstack/storage/migration_source.py` (repoint `V3_SOURCE_FILES`), `workstack/storage/validation.py` and `workstack/storage/repository.py` (repoint legacy markers), `workstack/storage/experimental_application.py` (pin the v3 roster), `workstack/owner_authority.py` (if the roster loop needs it), `workstack/agent_authority.py` (accept 5), `tests/test_report_documents.py`, `tests/test_store_schema_v5_migration.py`, `tests/test_v4_adapter_roster_frozen.py` | the only batch that may touch `store.py`; §3, §4, §5, §6, §7, §9 and §10 must land as **one** design |
| **B2** HTTP | `workstack/report_documents_http.py` (new), `workstack/server.py` route registration only, `tests/test_report_documents_http.py` | must ship GET list and GET read together with the mutations; the UI cannot be written against mutations alone |
| **B3** client API | `frontend/src/api/client.ts` and its test | one owner, once |
| **B4a** promotion UI | `frontend/src/features/review/reportDocumentActions.tsx` (new) and test, one footer action in `DailyReportDraftEditor.tsx` | |
| **B4b** list/archive UI | `frontend/src/features/review/ReportDocumentList.tsx` (+test, +css) | parallel with B4a only after B3 lands; archive and restore are mutations, so it depends on B2 |

Frozen, read-only for every batch: `workstack/reporting.py`, `workstack/reporting_http.py`,
`workstack/storage/semantic.py`, `docs/DAILY-REPORT-PREVIEW-CONTRACT.md`,
`docs/SSOT-STORAGE-V4-ADR.md`, `frontend/src/features/review/reportDraftStorage.ts`,
`reportDraftEditorModel.ts`, `useReportDraft*.ts`.

---

## 16. Tests required

**B1 — roster and migration**

1. A v3 nine-file authority becomes a v5 ten-file authority; the nine payloads compare
   equal in content; `store-meta.json` carries the `reports` evidence record with
   `origin: "migrated_v3"`.
2. **A healthy nine-file v3 store is accepted, not refused** — the regression the first
   draft would have shipped. Asserted directly against `initialize()`.
3. v1-to-v5 and v2-to-v5 chains each preserve the existing migration's semantics and finish
   at ten documents with the correct `origin`.
4. `V1_DOCUMENT_NAMES`, `V2_DOCUMENT_NAMES` and `V3_DOCUMENT_NAMES` equal their literal
   frozen sets, and `set(DEFAULTS) == V5_DOCUMENT_NAMES`.
5. Interrupted migration at all three points of §7.3.
6. Old build refusal: `store_schema_version = 5` read by a `STORE_SCHEMA_VERSION = 3`
   validator raises "newer than this Work Stack build" and writes nothing.
7. Manifest file set equals `DEFAULTS`; a new backup archive has ten payload members and a
   manifest recording schema 5.
8. Pre-migration backup: taken against a v3 authority it is a **nine**-member archive
   recording schema 3, and it verifies under v3 rules on a v5 build.
9. Restore of a nine-member v3 archive into a v5 build produces a v5 authority whose
   metadata says 5, with an empty `reports.json` and `migrated_v3` evidence — not a v3
   metadata document with a tenth file beside it.
10. Restore refuses: an archive from another workspace; an archive claiming a schema newer
    than the build; a `reports.json` whose `workspace_uid` disagrees with the destination.

**B1 — v4 non-regression**

11. `storage migration plan|preview|execute|verify` on an unmigrated v3 store produces
    byte-identical output before and after this change.
12. `ExperimentalV4StoreAdapter.load("reports.json")` raises
    `V4_APPLICATION_DOCUMENT_UNKNOWN`, not `KeyError`; `_emit_refresh_event` lists exactly
    the nine v3 names.
13. `to_v3_documents()` still returns nine documents with `store_schema_version: 3`.

**B1 — domain**

14. Every mutation leaves the other nine documents byte-identical.
15. CAS: a stale `expected_revision` refuses and changes nothing — asserted for revise,
    finalize, archive **and** restore, which is also the assertion that all four advance
    `revision`.
16. The §5.3 table exhaustively: every (state, operation) pair, including revise on a
    finalized document reopening it to `draft` with `reopened: true`.
17. Restore collision: archive, create a new document for the same period, then restore
    gives `report_duplicate_period` and writes nothing.
18. Idempotency: same key and same body returns the **recorded** status and body with
    `replayed: true` and creates no second content revision — asserted both after the
    source day changed and after later finalize/archive mutations; same key with a
    different body refuses.
19. Ledger: age pruning works; a ledger of 1_000 unexpired records refuses the next
    mutation with `report_idempotency_capacity` and deletes nothing.
20. Limits: content-revision cap, document cap and byte cap each refuse without evicting;
    finalize, archive and restore still succeed at the content cap.
21. `source_digest` recorded at creation never changes when the source day changes.
22. Deep validator: each §10.1 clause refuses a hand-built `reports.json`, content-free.

**B2**

23. Admission order — UID mismatch, then sync, then key, then body — each refusing
    content-free, asserted by spoiling one input at a time.
24. `workspace_uid` missing from the query, repeated in the query, or disagreeing with the
    body, on every route.
25. Missing or invalid `Idempotency-Key` on every mutation route.
26. GET list: the frozen `(updated_at DESC, uid ASC)` order, `omitted_count`,
    `state=active` excluding archived, `state=archived` listing them, and a cursor reused
    with a changed filter refusing with `report_cursor_invalid`.
27. `GET /api/v1/reports/daily-preview` output is byte-identical before and after report
    writes.

**B3/B4**

28. Promotion failure keeps the local text and both fallbacks (Copy, Download).
29. Promotion success keeps the local draft and shows local and server labels separately.
30. Stale source is shown, never auto-refreshed.
31. Markdown containing `<script>` and pipe tables renders as characters, no element.

---

## 17. Rollback-safe implementation steps

Each step is independently revertible and leaves a working product.

1. Land the **frozen roster constants** and repoint every historical consumer
   (`_migrate_v1_locked`, `_validate_v2_auxiliary_stores`, `migration_source`,
   `validation`, `repository`, `experimental_application`) **while `DEFAULTS` is still nine
   documents**. This is a pure refactor: no behaviour changes, every existing test passes
   unchanged, and it reverts on its own. It is the step that makes the rest safe.
2. Land the version-aware backup/verify/restore seam in `maintenance.py`, still at nine
   documents, with the version threaded from the archive manifest. Also a pure refactor.
3. Land the roster entry, `STORE_SCHEMA_VERSION = 5`, the deep validator, the metadata
   evidence record and the migration **in one commit**. They cannot be split: a store
   carrying `reports.json` at version 3 is the silently-widened roster this contract exists
   to prevent, and a version bump without the roster refuses every existing authority. The
   migration takes its version-correct verified backup before the write, so revert equals
   restore that backup.
4. Land `report_documents.py` — pure model, state machine and validators — with no callers.
   Revert equals delete.
5. Land the transactional operations against `DocumentRepository`, with tests. Still no
   HTTP surface, so no user-visible change. Revert equals delete.
6. Land `report_documents_http.py` plus the route registration lines. Revert equals
   removing the registrations; the module becomes dead code and the store is unaffected.
7. Land `client.ts` functions unused by any component. Revert equals delete.
8. Land the promotion action, then the list UI. Each revert is a UI-only change; authored
   documents stay readable through the API.

Steps 1 and 2 are the review's central instruction: **the roster and backup seams are
separated from the schema change, and land first.**

---

## 18. Out of scope, and named

Out of scope for this contract: AI or LLM generation of any kind, scheduling, background
regeneration, network egress, new dependencies, multi-user or team sharing, multi-project
platform behaviour, and deletion of authored reports.

Deferred with an owed artifact, so the deferral is visible rather than implied:

| Deferred | Owed |
|---|---|
| weekly reports | a canonical weekly source-digest contract; until it exists `template` is `daily-v1` only and `period.kind` is `day` only (§8.4) |
| v5 report documents under `workstack.ssot` v4 | `docs/REPORT-DOCUMENT-V5-TO-V4-CONVERSION.md`: record family, stream shape, ledger placement, algorithm version (§6.3) |
| reports through the experimental v4 adapter | refused content-free as `report_capability_unavailable` until that conversion exists (§6.2) |
