# Capture observation storage contract

What an operator needs to know about the `captures.json` version 2 container:
when it appears, what backs it up, and what it does **not** prove.

## Two admissible containers under collection schema 6

| Container | Shape | Written by |
| --- | --- | --- |
| 1 | `{"version": 1, "captures": [...]}` | every fresh store, and every store that has never recorded an observation |
| 2 | `{"version": 2, "captures": [...], "observations": [...]}` | only the explicit `Store.record_capture_observation` write |

Collection versions 1, 2, 3 and 5 keep container 1 and nothing else; a
container 2 document inside one of those rosters is refused. Under version 6
both are admitted, and every other shape — an unknown container version, a
version that is not an integer, a version 2 document with an extra or a
missing field — is refused as store corruption.

A stored container 2 must already **be** the canonical observation list the
model returns — sorted by Capture id — and not merely a list the model would
accept and reorder: a stored list that normalizes to something else is refused
as store corruption rather than admitted, opened or quietly repaired, so an
identical record replay against admitted authority always stays a complete
no-op. Container 2 has never shipped, so no existing store can hold a
noncanonical one.

There is no new roster file, no store schema 7 and no new Capture Packet
version. `observations` is an owner-internal list holding at most the last
accepted source-check observation per Capture; eviction is deliberate and
means "no saved check", never "unknown authority".

## Nothing converts on its own

Initializing, opening, loading and `consistent_read` never convert container 1
to container 2, and never write a backup merely because a newer build is
installed. The v5-to-v6 schema upgrade planner is not involved in this change
at all. The **only** thing that activates container 2 is an owner calling
`Store.record_capture_observation` with a record the runtime already admitted.

## What the first activation does

The first real 1 → 2 write, under the owner's transaction and in this order:

1. reads all eleven schema 6 documents once and validates them normally;
2. validates the record against the workspace uid **these documents** declare,
   and requires the bound Capture id to exist at exactly the bound revision;
3. proves the held bytes are still the generation the runtime manifest
   recorded (the released `_assert_upgrade_source_owned_locked` gate);
4. writes a rollback archive of those exact eleven original documents into
   `.workstack-migration-backups/`, reads it back, verifies it, and re-binds it
   at its final path;
5. commits the upgraded document **and** the new record through one `save_many`.

Any refusal in steps 1–4 happens before the recovery journal exists, so all
eleven authoritative documents are left exactly as they were found. A verified
rollback archive may already exist at that point; it is not authoritative data.

Once the journal exists the write may complete on recovery. After a crash at an
unknown point, reopen the store: recovery either finishes the intended
container 2 or leaves a diagnosable journal. Do not assume "nothing was saved",
and do not re-run a source check to find out — read the stored state.

Subsequent writes to an already-version-2 container are ordinary journalled
updates of the bounded list. **No second format backup is taken**, because no
format changes. Recording a record the store already holds, byte for byte, is a
no-op: no write, no backup, no journal entry.

## Deployment and downgrade boundary

A build older than this feature cannot read a container 2 `captures.json`; it
refuses it. This is a genuine one-way deployment boundary, not a
backward-readable format, and no downgrade conversion is offered. To go back,
restore the rollback archive written in step 4 — it holds the exact original
container 1 bytes.

Backup and restore both work on container 2: `backup_store` /`verify_backup` /
`restore_store` carry the observations through unchanged.

Old HTTP clients see no change. This slice adds no output field, so the Capture
list projection on the wire is exactly what it was.

## What this storage primitive does not prove

`Store.record_capture_observation` is a **trusted internal storage primitive**,
not an authority verifier and not a public security boundary:

- it does not recompute or re-prove the record's `capture_digest`;
- it does not re-admit the connection policy or re-run a verifier;
- no HTTP or CLI route reaches it in this slice.

The future runtime caller owns re-admitting authority and comparing the
retained binding **while holding the same outer Store transaction** this method
nests inside. A stored observation is a past accepted exchange, validated at
its own `accepted_at`; it is never evidence that a source is currently
accessible, and "matched when checked" can never be shown as "current".

A record naming a Capture this store does not hold, or a revision it has moved
past, is refused with a bounded code (`unknown_capture`,
`capture_revision_mismatch`). That is a statement about the submitted record,
not a claim that the workspace is damaged. Malformed *stored* documents keep
the store's ordinary corruption error.
