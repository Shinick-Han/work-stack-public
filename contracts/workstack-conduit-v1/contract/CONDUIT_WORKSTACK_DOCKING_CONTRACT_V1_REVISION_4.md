# Conduit ↔ Work Stack Docking Contract v1 — Revision 4 Consensus Candidate

**Date:** 2026-08-29  
**Status:** `REVISION_4_PENDING_BILATERAL_ACCEPTANCE`  
**Implementation authority:** None  
**Contract authority:** Not frozen  
**Reviewers:** Conduit codebase owner; Work Stack codebase owner; command-tower reconciler

## 1. Purpose

This document proposes the smallest durable contract that lets Work Stack hand a planning task to Conduit without merging the products, sharing a database, or creating a second authority for either product's state.

The proposal is intentionally transport-neutral and provider-neutral. Work Stack produces one immutable planning-task snapshot. Conduit validates that exact snapshot, lets the user review and classify the work, and may create a new Conduit execution task. The two products remain independently buildable, testable, and releasable.

This candidate is not permission to implement. Revision 3 was accepted by both product owners, but the final safety-policy sweep measured a cross-runtime NFC disagreement. Revision 4 changes only the normative Unicode normalization version, its canonical-encoding cross-reference, the corresponding conformance-fixture requirement, review coordinates, and revision labels. Each product owner must review the complete Revision 4 bytes and the paired safety-policy Revision 5 root. Any further material amendment must be reviewed again by both sides before the contract is frozen.

Review-history coordinates:

- original draft SHA-256: `d2b0e8e2d389fd8ff6e4b320cb358334b0d4434835ee4fcb6662f46b487d07a4`;
- Conduit receipt: `CONDUIT_WORKSTACK_DOCKING_CONTRACT_V1_CONDUIT_FEASIBILITY_REVIEW_2026-08-29.md`, SHA-256 `2cf9ef5afe572d223373f1725798d963144e620bebd6acebcf6b460b96e56b4c`;
- Work Stack receipt: `CONDUIT_WORKSTACK_DOCKING_CONTRACT_V1_WORKSTACK_FEASIBILITY_REVIEW_2026-08-29.md`, normalized repository copy SHA-256 `eb21a7d2fcbaa5e9af46434124747a87c6601ad021e99e0126d302f0986f612e`.
- Revision 2 candidate SHA-256: `e36dc36e8da07a330258b86ee2fb936726499c7b99178698bb7af1bdc53cffa0`;
- Conduit Round 2 receipt: `CONDUIT_WORKSTACK_DOCKING_CONTRACT_V1_CONDUIT_ROUND2_ACCEPTANCE_2026-08-29.md`, SHA-256 `569bd1c232aacb3d950e40c43448b6c0df5027122e400b9a7efb4b0f8c98d753`;
- Revision 3 candidate SHA-256: `c64cc4ade7af90437bbb6de172ddcc94932d1e5bae76ed7b839b5207bcd987f1`;
- Conduit Revision 3 acceptance receipt SHA-256: `f152de4c5fbb20934ad2a484ab0497030727250056751794610d40c47c6a8e3c`;
- Work Stack Revision 3 acceptance receipt SHA-256: `96ea0ccb0c6570169318e71e70eb9a8824811e9a3354191daac9dd23e75cb572`;
- bilateral Revision 3 text-acceptance record SHA-256: `3aec747e37b8e7146840de8e11f1201c8044cadecd94499adcdb3b96469c1e08`;
- Safety Policy Revision 4 Conduit ratification SHA-256: `0dc86868a3a6db1202607a855535b2714b268a18ed62125564ff5cbc5f27cfa3`;
- Safety Policy Revision 4 Work Stack amendment receipt SHA-256: `816e8b71774db3ef81ce91e61e722ff802f2fd17b73b09f8c1a7290b84184ed0`.

## 2. Current codebase assumptions to verify

### 2.1 Conduit

The current renderer's `create_task` request has a strict five-field shape:

- `expectedRevision`
- `title`
- `taskType`
- `dataClass`
- `risk`

It also assumes:

- Core is the sole semantic writer for Conduit execution state.
- The Core semantic writer is not in the Conduit product repository. It is a sealed, byte-pinned artifact built from the Platform repository, currently CPython 3.12/PyInstaller with protocol `conduit-core-stdio/4`. Task-schema changes are a two-repository migration plus artifact rebuild, re-seal, staging, and verification.
- The renderer may not supply workspace identifiers, actor identifiers, generated IDs, executable paths, seat data, provider data, or other authority-bearing fields.
- `create_task` preserves ambiguous-response semantics and must not be automatically retried.
- the current task projection has no planning-origin provenance and no full task description.
- adding description or origin data affects the Platform-repository event/writer schema, protocol codec, storage/projection decoder, host services, UI, byte-pinned fixtures, compatibility tests, restart evidence, and the sealed Core artifact pin set.
- `risk` is a closed enum: `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`, or `UNKNOWN`.
- `taskType` and `dataClass` are open lowercase tokens matching `^[a-z0-9][a-z0-9._-]{0,63}$`, not closed vocabularies.

### 2.2 Work Stack

The current Work Stack codebase has:

- a workspace UUID when `workspace.json` exists, but no completed fail-closed protection against regeneration beside nonempty planning data;
- a legacy human-readable task ID such as `T-0001`;
- persisted UUIDv5 planning-task identity and revision for new tasks, while legacy tasks currently synthesize missing values in projections instead of committing them;
- a versioned save path with per-task `revision`, while the legacy status path can bypass revision and invalid legacy values can collapse to zero;
- planning fields including `title`, `detail`, `status`, `priority`, and `due`;
- planning statuses `open`, `started`, `done`, and `dropped`;
- priorities `P0` through `P3`;
- an existing capture helper that uses compact, sorted-key JSON and `sha256:<lowercase hex>`, but does not satisfy this contract's final-LF, NFC, escaping, or dedicated-snapshot requirements and must not be reused as the snapshot serializer;
- no completed versioned task-creation endpoint yet;
- no existing Conduit runtime or Conduit-state writer embedded in the product.

WS1 must commit stable workspace/task identity and strict revision semantics before snapshot production. Both reviewers must correct any remaining inaccurate assumption before accepting this contract.

## 3. Non-negotiable ownership boundary

### 3.1 Work Stack owns planning state

Work Stack is authoritative for:

- the Work Stack workspace UUID;
- the Work Stack planning-task UUID and legacy display ID;
- planning title and detail at a specific revision;
- planning status, priority, and due date;
- Work Stack objectives, dependencies, subtasks, notes, and future planning metadata.

Conduit must not mutate these values in Work Stack. A user editing a Conduit task after import does not edit the source planning task.

### 3.2 Conduit owns execution state

Conduit is authoritative for:

- Conduit workspace and task identifiers;
- execution task title and description after confirmation;
- task type, data class, and risk classification;
- rooms, seats, assignments, runs, execution assets, artifacts, evidence, gates, and outcomes;
- provider, harness, model, authentication, session, dispatch, and process state;
- the append-only event history and projections for those concepts.

Work Stack must not mint, predict, or mutate any of those values.

### 3.3 No shared mutable authority

Version 1 explicitly forbids:

- a shared database;
- bidirectional status synchronization;
- background creation of Conduit tasks;
- automatic Work Stack status changes after Conduit activity;
- Work Stack writing directly to the Conduit event store;
- Conduit writing directly to Work Stack storage;
- a combined object whose fields are independently mutable by both products;
- provider credentials, session tokens, executables, environment variables, or raw provider traffic in the docking payload.

## 4. V1 interaction model

The complete v1 interaction is one-way and user-mediated:

1. Work Stack exports an immutable snapshot for exactly one planning task at exactly one revision.
2. Conduit reads the exact snapshot bytes and validates the envelope, canonical encoding, digest, field constraints, and privacy rules.
3. Conduit displays a review screen. No Core mutation has occurred yet.
4. The user reviews or edits the proposed Conduit title and description and explicitly selects Conduit-owned `taskType`, `dataClass`, and `risk` values.
5. The user explicitly confirms creation.
6. Trusted Conduit code submits one atomic creation request to Core containing user-controlled Conduit fields plus trusted, immutable source provenance.
7. Core either records the task and provenance atomically or records neither.
8. Work Stack remains unchanged.

There is no automatic spawn, dispatch, provider authentication, or run start in this contract.

## 5. Transport boundary

### 5.1 Contract bytes, not a particular transport

The normative contract is the byte representation defined in section 7. File export/import is the first required transport because it is inspectable, reversible, and does not require either product to host a new network authority.

A later loopback HTTP endpoint may carry the same exact bytes, but it must not introduce alternate fields, alternate normalization, or alternate trust semantics. HTTP support is therefore a transport adapter, not a second contract.

### 5.2 Initial file convention

Recommended names:

- snapshot: `<planning-task-uid>.workstack-task.json`
- optional digest sidecar: `<planning-task-uid>.workstack-task.json.sha256`

The sidecar, when present, contains exactly the lowercase digest label plus a final LF:

```text
sha256:<64 lowercase hexadecimal characters>\n
```

The consumer always recomputes the digest from the snapshot bytes. A sidecar, HTTP header, ETag, filename, or UI label is never authoritative by itself.

## 6. Normative snapshot schema

### 6.1 Format identifier

The exact format identifier is:

```text
workstack.planning-task-snapshot.v1
```

### 6.2 Exact object shape

The top-level JSON value is an object with exactly these keys and no others:

```json
{
  "detail": "Explain the desired work without credentials or raw provider traffic.",
  "due_date": null,
  "format": "workstack.planning-task-snapshot.v1",
  "legacy_task_id": "T-0031",
  "origin_ref": "workstack://0f50a123-3da8-4c82-8f16-8ee1a57260c4/planning-tasks/2e82845c-bccb-5aa6-9b6d-8ec65170c00a",
  "planning_priority": "P1",
  "planning_status": "open",
  "planning_task_uid": "2e82845c-bccb-5aa6-9b6d-8ec65170c00a",
  "revision": 3,
  "title": "Prepare the provider-neutral execution adapter",
  "workspace_uid": "0f50a123-3da8-4c82-8f16-8ee1a57260c4"
}
```

The following are deliberately absent from v1:

- objectives;
- dependencies;
- subtasks;
- notes;
- tags;
- attachment paths;
- capture or reply payloads;
- generated timestamps;
- Conduit task type, data class, or risk;
- provider, harness, model, authentication, session, or process data;
- executable paths, environment variables, or credentials.

Their exclusion keeps the first contract stable while their identity and privacy semantics are still product-specific. Adding any field requires a new format version.

### 6.3 Field rules

For format v1, `NFC` means Unicode Normalization Form C evaluated with the Unicode Standard 17.0.0 normalization data and algorithm. Producer, consumer, canonical-byte fixture generators, and conformance harnesses must produce the Unicode 17.0.0 result independent of their host runtime's bundled Unicode version. A runtime normalization API backed by any other Unicode version is not conforming unless its result is proven byte-for-byte equivalent for the input being checked.

| Field | Normative rule |
|---|---|
| `format` | Exact literal `workstack.planning-task-snapshot.v1`. |
| `workspace_uid` | Lowercase, hyphenated, non-nil RFC 4122 UUID string. UUID version is not constrained. Non-authority provenance metadata in Conduit. |
| `planning_task_uid` | Lowercase, hyphenated, non-nil RFC 4122 UUID string. UUID version is not constrained. Non-authority provenance metadata in Conduit. |
| `legacy_task_id` | Exact stored Work Stack value matching `^T-[0-9]{4,}$`; never caller-supplied or case-normalized. Display metadata, never authority. A noncanonical stored value requires explicit migration or repair before export. |
| `origin_ref` | Exact deterministic value `workstack://<workspace_uid>/planning-tasks/<planning_task_uid>`. No alternate escaping or query fields. This is the WS1 target grammar and is non-authority provenance metadata in Conduit. |
| `revision` | JSON integer from `0` through `9007199254740991`. |
| `title` | Exact committed Work Stack value at `revision`; already NFC; 1–256 Unicode scalar values and 1–256 UTF-16 code units; no C0 controls (U+0000–U+001F), no DEL (U+007F), no C1 controls (U+0080–U+009F). Producer and consumer reject rather than normalize. |
| `detail` | Exact committed Work Stack value at `revision`; already NFC; 0–4096 Unicode scalar values and 0–4096 UTF-16 code units. LF (U+000A) and horizontal tab (U+0009) are permitted; all other C0 controls (U+0000–U+001F), DEL (U+007F), and C1 controls (U+0080–U+009F) are forbidden. Producer and consumer reject rather than normalize. Conduit must define a new Core text class for this field because the current common text rule does not admit LF/TAB. |
| `planning_status` | One of `open`, `started`, `done`, `dropped`. |
| `planning_priority` | One of `P0`, `P1`, `P2`, `P3`. |
| `due_date` | `null` or a real Gregorian civil planning date formatted exactly `YYYY-MM-DD`. It conveys no timezone, instant, execution deadline, or scheduling authority. |

Neither producer nor consumer may silently truncate, coerce, normalize, repair, substitute, or drop an invalid field. Work Stack returns `SNAPSHOT_FIELD_INVALID` with the field name and measured bound, without copying sensitive content into the diagnostic. The user must edit and commit the Work Stack task, producing a new revision, before export. An export-only edit is forbidden.

`origin_ref`, `legacy_task_id`, `workspace_uid`, and `planning_task_uid` are non-authority provenance metadata everywhere inside Conduit. They must never be parsed by an authority parser, select or admit an operation, become a capability, or widen an existing Conduit reference grammar.

### 6.4 Content safety

The snapshot may describe internal work, but it must not contain:

- access tokens, API keys, passwords, private keys, session cookies, or authorization headers;
- raw provider request or response bodies;
- raw capture or reply content whose disclosure policy has not been evaluated;
- credential-bearing command lines;
- environment assignments or blocks containing values; or
- machine-local paths that expose user, credential, or secret locations.

Ordinary command names, environment-variable names, email addresses, and technical task descriptions are not inherently forbidden.

Producer and consumer must use the same `snapshot-v1` content-safety policy and fixture hash from the conformance kit. This policy combines the exact structural schema with a narrowly scoped, high-confidence credential tripwire; it does not claim comprehensive secret detection. Its exact positive and negative cases must be ratified by both product owners before the kit freezes. A positive match is a hard refusal, automatic redaction is forbidden, and diagnostics must not echo the matched content.

Before file creation, Work Stack must display the exact exported `title` and `detail`, disclose that objectives, dependencies, subtasks, notes, and tags are omitted, and require an explicit disclosure confirmation. Privacy refusal and cancellation do not mutate the task.

## 7. Canonical byte representation and digest

### 7.1 Encoding

The snapshot is encoded as:

- UTF-8;
- no byte-order mark;
- one JSON object;
- keys sorted by Unicode code-point order;
- compact separators: `,` and `:` with no insignificant whitespace;
- after validation, non-ASCII Unicode scalar values emitted directly as UTF-8;
- `"` escaped as `\"`, `\` escaped as `\\`, LF escaped as `\n`, and horizontal tab escaped as `\t`;
- `/` not escaped and ordinary scalar values not emitted as `\uXXXX`;
- unpaired surrogate values rejected;
- all source strings already NFC-normalized under the Unicode 17.0.0 rule in section 6.3 before serialization;
- exactly one LF byte (`0x0A`) after the closing brace;
- no bytes after that LF;
- binary-mode output on every platform.

The valid example in section 6 is illustrative and pretty-printed. Normative fixture bytes will be compact.

### 7.2 Digest

The digest is SHA-256 over the exact snapshot bytes, including the final LF. Its textual representation is:

```text
sha256:<64 lowercase hexadecimal characters>
```

The consumer hashes the received bytes before parsing. It must not parse and reserialize the object to decide whether a supplied digest matches. After parsing, it separately verifies that the received bytes equal the canonical serialization of the parsed value. This catches BOMs, CRLF endings, alternate whitespace, duplicate-key ambiguity, and serializer drift.

### 7.3 Duplicate keys and numeric form

Duplicate JSON keys are forbidden. `revision` must use the shortest ordinary base-10 integer spelling with no sign, leading zero, exponent, or fractional part.

### 7.4 Size

The complete snapshot, including the final LF, must not exceed 65,536 bytes.

## 8. Work Stack producer obligations

Work Stack must:

1. operate only on an initialized, fully recovered, fully WS1-migrated store; refuse with `SNAPSHOT_STORE_NOT_READY` if initialization, recovery, or migration would be required;
2. read one committed planning task and one committed workspace identity under one consistent transaction;
3. copy one exact committed task record and freeze the values at one task revision;
4. validate all v1 fields and content-safety rules;
5. derive `origin_ref` rather than accepting it from a caller;
6. emit deterministic canonical bytes;
7. compute the digest over those exact bytes;
8. expose a user-visible review and export action;
9. make export read-only with respect to all planning state, Activity, idempotency records, and journals;
10. produce byte-identical output whenever the task revision and normative fields are unchanged;
11. refuse export if identity migration, revision, validation, privacy, or consistent-read checks are incomplete.

Work Stack must not claim that export created, reserved, or linked a Conduit task.

Snapshot immutability means immutability of the emitted bytes and digest. Work Stack v1 does not retain historical snapshot records or promise reconstruction of an earlier revision after the source task changes. The requested external snapshot and optional sidecar are the only permitted writes by the export operation.

## 9. Conduit consumer obligations

### 9.1 Trusted ingestion

Snapshot bytes must be ingested and validated in trusted Conduit code. The renderer may display validated facts but must not manufacture source provenance.

The trusted ingestion path must verify, in order:

1. byte length;
2. supplied digest syntax, if present;
3. SHA-256 of the exact bytes;
4. UTF-8 validity and absence of BOM;
5. exactly one terminal LF and no trailing bytes;
6. duplicate-key refusal;
7. exact top-level key set;
8. all field constraints;
9. deterministic `origin_ref` derivation;
10. canonical reserialization equality;
11. pinned privacy and credential rules.

Validation failure must not create a Core task, ticket, room, seat, run, provider process, or session.

### 9.2 Import ticket

After validation, trusted main-side code must mint an opaque, short-lived, one-use import ticket. The ticket:

- conforms to the frozen opaque-reference grammar `^[a-z][a-z0-9]{1,15}_[0-9a-f]{32,64}$` under a new contract-specific prefix;
- is minted only from trusted main-side randomness and never solely from producer-controlled bytes such as the snapshot digest;
- binds the validated digest, `origin_ref`, source revision, proposed title/detail, and a workspace/window identity derived through Conduit's existing single authority-join seam;
- carries explicit issue and expiry instants under one frozen lifetime constant;
- is consumed using delete-before-await so a concurrent second presentation cannot succeed;
- is invalid after expiry, cancellation, successful consumption, workspace rebinding, process restart, or generation retirement;
- is classified under Conduit's retained-record policy together with any duplicate-warning index, including explicit drain-or-persist behavior.

The renderer receives the opaque ticket and display values, not authority to submit arbitrary provenance. It may never supply the workspace/window binding or ask a second resolver to derive it.

### 9.3 Review and classification

The user must be able to:

- inspect the Work Stack source reference, source revision, and digest;
- see `planning_status`, `planning_priority`, and `due_date` only as source planning metadata, with a disclosure that objectives, dependencies, subtasks, notes, and tags are omitted;
- review or edit the proposed Conduit title;
- review or edit the proposed Conduit description;
- explicitly select `taskType`, `dataClass`, and `risk` under existing Conduit validation rules;
- cancel without mutation;
- explicitly confirm one creation attempt.

Import must not automatically create, spawn, assign, dispatch, authenticate, or start anything.

`planning_status`, `planning_priority`, and `due_date` must not preselect, infer, or mutate Conduit lifecycle, `taskType`, `dataClass`, `risk`, scheduling, or execution state.

### 9.4 Atomic Core record

The created Conduit task must atomically include Conduit-owned fields and immutable origin provenance equivalent to:

```json
{
  "kind": "WORKSTACK_PLANNING_TASK_SNAPSHOT",
  "origin_ref": "workstack://0f50a123-3da8-4c82-8f16-8ee1a57260c4/planning-tasks/2e82845c-bccb-5aa6-9b6d-8ec65170c00a",
  "snapshot_digest": "sha256:<64 lowercase hexadecimal characters>",
  "source_revision": 3
}
```

Core must expose a separate trusted `create_task_from_planning_snapshot` command in a new protocol generation. The renderer-facing import request has an exact field set consisting of `expectedRevision`, user-reviewed `title`, user-reviewed `description`, `taskType`, `dataClass`, `risk`, and opaque `ticketRef`. Trusted main code consumes the ticket, resolves the immutable source provenance, mints or supplies the per-confirmation trusted attempt identity defined by §9.7, and composes the Core frame. The renderer never receives or supplies provenance, attempt identity, workspace identity, or Core-generated IDs.

The existing five-field `create_task` command and its denylist remain unchanged. Simply adding provenance or description to that existing renderer-authoritative allowlist is not acceptable.

### 9.5 Projection and compatibility

Conduit must project enough immutable provenance to recover from an ambiguous creation response. Existing tasks created before this contract must remain readable and project an explicit absence of description/origin, such as `null`, through a versioned compatibility path. Existing event bytes must not be silently reinterpreted.

Old-task compatibility follows the existing side-by-side generation model: old projections are read as their original generation without upcasting or hash recomputation, while decoded records expose explicit `null` for absent description/origin. The Platform Core design must record whether the new Task generation adds a per-record format discriminator or keys decoding on projection protocol generation.

User edits during review make the resulting Conduit title and description authoritative in Conduit. Provenance means “derived from this reviewed snapshot,” not “all current task text remains byte-identical to Work Stack.”

### 9.6 Duplicate intent

The contract does not declare a global one-to-one relationship. The same snapshot may intentionally produce zero, one, or multiple Conduit tasks, but each task requires a distinct explicit confirmation, valid ticket, and per-confirmation trusted attempt identity. The UI must warn when the same digest or `origin_ref` is already visible in the current projection. This warning is informational and never an admission or deduplication authority.

### 9.7 Ambiguous response

If Core may have accepted a creation but the response is ambiguous:

- do not automatically retry;
- invalidate or quarantine the consumed ticket;
- query using a per-confirmation trusted attempt identity carried into the Core command, or Core's own replay/receipt identity, through a keyed Core query with exactly-one semantics;
- use `snapshot_digest` and `origin_ref` only as corroborating display facts, never as the recovery key;
- report `CREATED`, `NOT_CREATED`, or `UNKNOWN` based only on authoritative evidence;
- require a new user decision before any new attempt.

Before this clause can freeze, the Platform Core owner must verify from Core source or an authoritative protocol record: byte-identical receipt identity for exact replay; field-match-conditioned read-only reconciliation after `STALE_REVISION` or `CONFLICTING_REPLAY`; and any digest composition relied on by recovery. Conduit must also preserve typed refusal information through IPC rather than collapsing it into a generic rejection.

## 10. Explicitly deferred capabilities

The following are not part of v1:

- Conduit-to-Work-Stack status updates;
- automatic closing or starting of planning tasks;
- a mutable cross-product link table;
- objective, dependency, subtask, note, tag, or attachment import;
- bulk import;
- automatic room, seat, run, or provider selection;
- remote cloud relay;
- continuous background synchronization;
- provider-specific fields;
- shared authentication or credentials;
- use of the imported description as agent or task execution input;
- a guarantee that one planning task maps to exactly one execution task.

A future immutable link receipt or status observation must use a separately reviewed versioned contract. It may not retroactively change v1 semantics.

## 11. Versioning and compatibility policy

- The `format` literal is the version discriminator.
- Unknown fields are rejected, not ignored.
- Missing fields are rejected, even if a future implementation could infer them.
- Changing a field name, type, enum, normalization rule, limit, canonicalization rule, or authority rule requires a new format version.
- A consumer may support multiple explicit versions, but each version has its own exact validator and fixtures.
- Producer and consumer releases must declare the contract fixture-bundle hash they passed.
- A transport adapter may be added without a format revision only when it carries the exact same bytes and trust semantics.

## 12. Shared conformance kit required before implementation

After bilateral agreement, the command tower will freeze a shared kit containing:

1. the final contract document;
2. a machine-readable JSON Schema used as a structural aid, not a replacement for semantic validation;
3. at least two valid canonical byte fixtures, including Unicode;
4. expected SHA-256 labels for every valid fixture;
5. invalid fixtures covering:
   - BOM;
   - invalid UTF-8;
   - missing or extra terminal newline;
   - CRLF or pretty-printed/noncanonical JSON;
   - duplicate keys;
   - unknown or missing fields;
   - invalid UUIDs;
   - `origin_ref` mismatch;
   - invalid revision forms and bounds;
   - invalid status, priority, or date;
   - empty or overlong title;
   - overlong detail;
   - forbidden controls;
   - credential/privacy canaries;
   - forbidden Conduit/provider authority fields;
   - incorrect digest;
   - non-ASCII NFC-sensitive strings;
   - a Unicode-version discriminator whose Unicode 17.0.0 NFC result differs from legacy runtime normalization data;
   - astral/non-BMP title and detail characters that distinguish scalar-value and UTF-16-code-unit bounds;
   - lone-surrogate refusal;
   - snapshot-v1 safety-policy positive and negative controls, including legitimate email addresses, command names, environment-variable names, and technical prose;
6. language-neutral acceptance notes;
7. a manifest containing the SHA-256 of every kit file and the bundle root hash.

Canonical-serialization notes must cite section 7.1 rather than restating it. Both repositories must pin and test the exact same frozen bundle and safety-policy fixtures. Neither repository may maintain a hand-edited semantic fork.

## 13. Revision 4 bilateral acceptance review

Both product reviewers must review the complete Revision 4 bytes and paired Safety Policy Revision 5 root, not only the Unicode change summary.

### 13.1 Conduit reviewer

The Conduit reviewer must explicitly confirm or amend:

1. the two-repository Platform Core + TypeScript product migration topology;
2. the separate new-generation `create_task_from_planning_snapshot` command;
3. the exact seven-field renderer-facing request in §9.4, including user-reviewed `description` as content but no renderer-supplied provenance;
4. opaque ticket grammar, randomness, delete-before-await consumption, single authority-join binding, retirement, and retention classification;
5. per-confirmation attempt identity and keyed exactly-one ambiguous-result recovery;
6. side-by-side old-generation compatibility and the remaining Platform decision about Task format discrimination;
7. the prohibition on using imported description as execution input;
8. ordering relative to R3, W4, Platform migration, re-seal, and product adoption.

### 13.2 Work Stack reviewer

The Work Stack reviewer must explicitly confirm or amend:

1. WS1 identity/revision migration and fail-closed store readiness;
2. exact committed title/detail semantics and refusal-only editing workflow;
3. the dedicated canonical serializer and exact escaping rules;
4. consistent one-transaction reads and literal no-mutation export evidence;
5. explicit disclosure review and omission notice;
6. the meaning of emitted-artifact immutability without historical reconstruction;
7. WS1 → WS2 → WS3 → contract freeze → WS4 producer ordering;
8. the same `snapshot-v1` safety-policy direction and shared fixture requirement.

### 13.3 Required response from both reviewers

Each reviewer must return:

- exact Revision 4 SHA-256 and Safety Policy Revision 5 bundle root received;
- exact repository URL, branch, commit, tree, and dirty-state summary reviewed;
- verdict: `ACCEPT`, `AMEND`, or `REJECT`;
- explicit response to every product-specific item above;
- safety-policy status: `RATIFY`, `AMEND`, or `DEFER_PENDING_FIXTURES`;
- exact wording for any remaining amendment;
- confirmation that no implementation was performed.

## 14. Consensus and freeze procedure

1. The command tower publishes this draft to both existing product-context workers.
2. Each worker performs a read-only codebase review and returns the structured response in section 13.3.
3. The command tower reconciles the responses into one revised contract and a decision log.
4. If a material field, authority rule, canonicalization rule, lifecycle rule, or compatibility rule changes, both workers review the complete revised document again.
5. Consensus exists only when both workers accept the same full document revision. Agreement on separate summaries is insufficient.
6. Before kit generation, the Platform Core owner returns a written verification from Core source or an authoritative protocol record of exact-replay receipt identity, field-match-conditioned read-only reconciliation after `STALE_REVISION`/`CONFLICTING_REPLAY`, and any digest composition relied on by §9.7.
7. Both product owners explicitly ratify the complete `snapshot-v1` content-safety policy and its positive/negative fixtures.
8. The command tower generates the conformance kit and records the final document SHA-256 and bundle root hash.
9. Both workers verify the frozen hashes and return a final acceptance receipt.
10. Only then may the status change to `FROZEN_FOR_IMPLEMENTATION`.
11. Separate Platform Core, Conduit product, and Work Stack implementation plans are then issued. Each plan may change only its own repository and must pass the shared conformance kit.

Silence, partial agreement, a passing prototype, or one-sided implementation does not freeze the contract.

## 15. Proposed product-specific implementation sequence after freeze

This sequence is informative until the contract is accepted.

### 15.1 Work Stack

1. Complete WS1 immutable identity/revision migration, including fail-closed missing-workspace behavior and idempotent restart recovery.
2. Complete WS2 idempotent `POST /api/v1/tasks` and Quick Add migration.
3. Complete WS3 append-only planning-status transition facts.
4. Freeze and pin this bilateral contract and the conformance-kit hashes.
5. Implement an isolated snapshot model, strict validator, canonical serializer, digest, safety-policy adapter, and shared fixture tests.
6. Implement read-only CLI/file export and prove all Work Stack stores, Activity, idempotency records, and journals remain byte-identical.
7. After file conformance passes, optionally add an exact-byte loopback GET endpoint and browser review/download action. Direct Work Stack-to-Conduit HTTP integration remains deferred.
8. Publish the supported contract-bundle hash in release evidence.

### 15.2a Platform Core semantic writer

1. Verify and record replay, reconciliation, attempt-identity, and digest-composition semantics required by §9.7.
2. Implement the new trusted command, Task description/origin schema, per-confirmation recovery identity, event/projection generation, keyed query, and receipt semantics.
3. Rebuild, re-seal, stage, verify, and publish the sealed host artifact with new pins.

### 15.2b Conduit product surface

1. Complete R3 and obtain W4 entry approval for W4-owned integration surfaces. Isolated validator/fixture work may proceed earlier only through its own approved packet.
2. Adopt the new sealed pins and protocol generation with parallel codecs, projections, and fixtures; do not edit old golden generations in place.
3. Implement the isolated byte validator, canonicality check, safety-policy gate, and shared fixture tests.
4. Implement trusted ingestion and opaque import-ticket lifecycle without Core mutation.
5. Add the review/classification UI and cancellation path.
6. Wire the atomic creation and keyed ambiguous-result recovery paths, including typed refusal carry-through.
7. Prove restart recovery, old-task compatibility, ticket retirement, duplicate warnings, privacy-safe diagnostics, and ambiguous-result handling.
8. Publish the supported contract-bundle hash in build/test evidence.

Work Stack may implement its WS4 producer after WS1–WS3 and contract freeze. The Conduit import integration may not begin until that producer is independently approved and Conduit has entered W4. This resolves the prior phrase “after WS4” without making WS4 depend on itself.

### 15.3 Joint acceptance

1. Export a frozen valid fixture through the real Work Stack product path.
2. Import the exact bytes through the real Conduit product path.
3. Confirm no mutation before explicit user confirmation.
4. Create one Conduit task and verify immutable origin after restart.
5. Demonstrate cancellation, invalid digest, privacy refusal, stale ticket, duplicate warning, and ambiguous-response recovery.
6. Confirm Work Stack planning state did not change.

## 16. Draft decision summary

The command tower proposes the following defaults for review:

| Decision | Draft choice |
|---|---|
| Direction | Work Stack → Conduit only |
| Unit | One immutable planning task at one revision |
| Initial transport | User-mediated file export/import |
| Future transport | Same exact bytes over loopback HTTP |
| Format | `workstack.planning-task-snapshot.v1` |
| Digest | `sha256:<lowercase hex>` over exact bytes including final LF |
| Canonical JSON | UTF-8, no BOM, sorted keys, compact separators, NFC strings, one final LF |
| Provenance authority | Trusted Conduit ingestion and one-use opaque ticket |
| Recovery authority | Per-confirmation trusted attempt or Core replay/receipt identity; never snapshot digest alone |
| Conduit mutation | One explicit, atomic, user-confirmed Core creation attempt |
| Back-sync | None in v1 |
| Shared mutable storage | Forbidden |
| Automatic spawn/dispatch | Forbidden |
| V1 planning fields | Identity, revision, title, detail, status, priority, due date |
| Deferred fields | Objectives, dependencies, subtasks, notes, tags, attachments |
| Description execution use | Forbidden in v1; display/review/storage only |
| Contract freeze | Both product reviewers accept the same full revision, Platform Core verification is recorded, the safety policy is ratified, and fixture hashes match |

## 17. Review outcome placeholders

### Conduit feasibility receipt

First-round verdict: `AMEND`. Receipt SHA-256: `2cf9ef5afe572d223373f1725798d963144e620bebd6acebcf6b460b96e56b4c`. Round-2 verdict: `AMEND` limited to DEL and UTF-16 wording/fixtures; receipt SHA-256 `569bd1c232aacb3d950e40c43448b6c0df5027122e400b9a7efb4b0f8c98d753`. Revision-3 acceptance: `ACCEPT`, receipt SHA-256 `f152de4c5fbb20934ad2a484ab0497030727250056751794610d40c47c6a8e3c`. Revision-4 Unicode-pin acceptance: `PENDING`.

### Work Stack feasibility receipt

First-round verdict: `AMEND`. Normalized repository receipt SHA-256: `eb21a7d2fcbaa5e9af46434124747a87c6601ad021e99e0126d302f0986f612e`. Revision-3 acceptance: `ACCEPT`, receipt SHA-256 `96ea0ccb0c6570169318e71e70eb9a8824811e9a3354191daac9dd23e75cb572`. Revision-4 Unicode-pin acceptance: `PENDING`.

### Reconciliation decision log

See `CONDUIT_WORKSTACK_DOCKING_CONTRACT_V1_RECONCILIATION_2026-08-29.md`, `CONDUIT_WORKSTACK_DOCKING_CONTRACT_V1_ROUND2_RECONCILIATION_2026-08-29.md`, and `CONDUIT_WORKSTACK_DOCKING_UNICODE_17_RECONCILIATION_2026-08-29.md`.

### Frozen contract coordinates

`NOT FROZEN`
