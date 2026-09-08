# Public snapshot manifest

- Source version: **1.0.11**, source preview updated 2026-09-08.
- Integrated source commit: `46ab115b22d993d1f12fb7730c8d336773473a22`.
- Previous public commit: `de7d764af2e48454a32e770efe66feaaaaccbba2`.
- Private Git history is not exported. This is a new commit on the existing public history.
- Only committed product files are copied. Local artifacts, credentials, CodeGraph state and coordinator-only material are excluded.
- Public README/manifest are maintained separately. Existing installer files and their hashes remain unchanged.
- This source update does not publish a new installer, tag or stable update manifest. The manual preview remains 1.0.8.
- Current capabilities and remaining connector work: [Knowledge integration status](docs/KNOWLEDGE-INTEGRATION-STATUS-2026-09-08.ko.md).
- Validation and limitations: [source snapshot validation](docs/SOURCE-SNAPSHOT-2026-09-08.md).

## Previous binary release record (historical)


- Version: 1.0.8, manual-install pre-release; stable update remains 1.0.7.
- Source repository: `Shinick-Han/work-stack` (private development history is not exported).
- Integrated source commit: `80c86281a4fbba2c5a4fa139d781d2ef6791e4a5`.
- Runtime build source commit: `a7ec29d79495eafe845e214dcef1942d9199c133`; subsequent integrated commit adds validation documentation only.
- Export: tracked product source on the existing public history, excluding local CodeGraph metadata and coordinator-only handoffs. Publication README, manifest and installation documentation are updated separately.
- Installer: `installer/WorkStack-Setup-1.0.8.ps1` (26,334,724 bytes).
- SHA-256: `9299b4d349f43615df791cc66095a75ba3821bf7f025a30a56c2975e37b08a65`.
- Reused the already built and installed artifact; did not replace it with an untested rebuild.
- Verified 228 embedded product-source/front-end-dist files against the integrated source (text CRLF/LF normalized); included wheel/sdist hashes match pinned requirements.
- 2026-09-07: source export audit and 32 installer contract tests passed.
- 2026-09-07: exact 1.0.7 -> 1.0.8 artifact upgrade, custom backup/config/SSOT byte preservation, installer rollback and post-install launcher rollback passed in isolated temporary installations.
- Previous integration evidence: `docs/WORKSTACK-1.0.8-VALIDATION-2026-09-06.ko.md`.
- Limitation: no complete unified G30/immutable automatic-release PASS receipt. This is not a stable-channel promotion or an assertion of company-environment certification.
- `installer/workstack-update.json` intentionally retains the previously published 1.0.7 stable manifest. No 1.0.8 stable update manifest is published with this preview.

Runtime user data, credentials, personal configuration and private Git history are not included. Tracked `data` documents are demo fixtures. Existing public 1.0.7 installer assets remain available for recovery; restoring older software also requires a compatible pre-upgrade data backup.

Install/upgrade guide: [Korean guide](docs/WORKSTACK-1.0.8-INSTALL-UPGRADE.ko.md).
