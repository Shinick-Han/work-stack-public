# Public snapshot manifest

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
