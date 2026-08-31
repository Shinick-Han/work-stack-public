# Work Stack → Conduit v1 shared conformance kit candidate

**Status:** `CANDIDATE_PENDING_BILATERAL_HASH_ACCEPTANCE`  
**Implementation authority:** None

This directory freezes the byte-level and semantic tests for
`workstack.planning-task-snapshot.v1`. It contains the bilaterally accepted Contract
Revision 4, the bilaterally ratified Safety Policy Revision 5, a structural JSON Schema,
two canonical snapshot byte fixtures with expected digests, deterministic invalid-case
construction recipes, and language-neutral acceptance notes.

The JSON Schema is a structural aid only. Passing it does not establish conformance.
The contract document, exact valid bytes, Unicode 17.0.0 NFC rule, origin derivation,
canonical reserialization check, digest rules, invalid recipes, and safety bundle are
normative together.

No file in this candidate authorizes product implementation. Both product owners must
independently reconstruct `MANIFEST.sha256` and `BUNDLE_ROOT.txt`, execute the kit,
and accept the same exact root before the contract can be marked
`FROZEN_FOR_IMPLEMENTATION`.

Bundle hashing:

1. Every file other than top-level `MANIFEST.sha256` and `BUNDLE_ROOT.txt` is a
   payload.
2. Payload paths use forward slashes and are sorted by ordinal UTF-8 path bytes.
3. Each manifest line is
   `<64 lowercase hex><two ASCII spaces><relative path><LF>`.
4. `BUNDLE_ROOT.txt` is
   `sha256:<SHA-256 of exact MANIFEST.sha256 bytes><LF>`.
5. Any payload-byte change voids both product acceptances and requires a new root.
