# Work Stack → Conduit snapshot-v1 safety-policy candidate

**Status:** `CANDIDATE_PENDING_BILATERAL_RATIFICATION`  
**Implementation authority:** None

This directory is the concrete policy/fixture input required by the accepted docking contract before the full conformance kit can freeze.

Files:

- `snapshot-v1-safety-policy.md` — normative decision algorithm and diagnostic rules;
- `snapshot-v1-safety-cases.json` — shared positive and negative cases;
- `snapshot-v1-text-boundary-cases.json` — language-neutral construction recipes for control, NFC, surrogate, scalar, and UTF-16 boundaries;
- `MANIFEST.sha256` — generated after the two files above are final for review;
- `BUNDLE_ROOT.txt` — generated after the manifest is final for review.

Positive cases store `fragments` rather than a fully assembled credential-shaped string. A conforming test harness concatenates the fragments in order with no delimiter and runs the policy only in memory. This prevents the control repository itself from containing a complete credential-shaped canary while preserving exact cross-language inputs.

This candidate does not claim comprehensive secret detection. It freezes only high-confidence refusal cases and explicit negative controls. Both product owners must review the same file hashes and return `RATIFY` or exact amendments before these bytes can enter the final conformance kit.

All JSON fixture objects use exact key sets. Unknown or missing keys are a harness refusal. `literal`, `repeat`, and `utf16_code_units` are mutually exclusive construction forms; the harness constructs the input in memory and never persists generated positive or invalid text.

Bundle hashing:

1. `MANIFEST.sha256` contains one LF-terminated line per payload file other than `MANIFEST.sha256` and `BUNDLE_ROOT.txt`.
2. Lines are sorted by ordinal UTF-8 relative-path bytes and have exact form `<64 lowercase hex><two ASCII spaces><relative path>`.
3. `BUNDLE_ROOT.txt` contains `sha256:<lowercase SHA-256 of the exact MANIFEST.sha256 bytes>` followed by one LF.
4. Any review amendment regenerates both files and requires both product owners to review the new root.
