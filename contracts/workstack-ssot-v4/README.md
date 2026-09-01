# Work Stack SSOT v4 contract artifacts

These Draft 2020-12 JSON Schemas describe the canonical, human-readable v4
storage package before any v4 runtime writer is enabled.

The schemas deliberately separate:

- stable Store and workspace metadata;
- independently changing Task, objective, capture, reply, and note records;
- planning-status, activity, and worklog stream events;
- content-free migration evidence and bounded runtime idempotency replay state.

`common.schema.json` owns reusable envelopes and scalar definitions. Instance
documents declare `format` and `schema_version`; schema documents declare the
JSON Schema dialect. All references are local to this artifact set so packaged
and offline validation never requires network access.

JSON Schema validates document shape. The Wave 1 cross-record validator is
responsible for invariants that JSON Schema cannot express safely, including:

- workspace ownership and UID uniqueness;
- UID bucket/path agreement;
- reference existence and absence of dependency cycles;
- equality of a planning event's `record_uid` and `task_uid`;
- contiguous stream sequence and digest chains;
- reply provider/capability/receipt agreement;
- revision monotonicity and planning-status transition semantics.

The read-only `storage validate <path>` command now applies both layers to v4
candidates and rejects malformed stream segments without modifying the target.

`migration-receipt.schema.json` and `idempotency-ledger.schema.json` are Wave 3
activation prerequisites, not new domain streams. The receipt excludes paths and
domain text and retains only identities, digests, counts, artifact state, and
verification outcomes. The ledger excludes request bodies and arbitrary
top-level metadata; it bounds record counts, strings, collections, HTTP fields,
expiry, and compaction policy while preserving the two existing replay forms.
Migration admission must additionally enforce a bounded artifact byte size and
reject expired or duplicate ledger keys before activating a candidate.

The examples are executable contract evidence. `examples/valid/cases.json`
contains one representative instance per concrete schema. The invalid cases each
violate one named, directly inspectable schema keyword, allowing standard-library
artifact tests to remain dependency-free.

`MIGRATION-MAPPING-V3-TO-V4.md` is the authoritative field-loss audit for these
drafts. A migration implementation may not activate a candidate until all
contract, cross-record, semantic-parity, and source-unchanged checks pass.
