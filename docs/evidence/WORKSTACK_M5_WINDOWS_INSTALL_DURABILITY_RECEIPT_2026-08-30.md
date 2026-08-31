# Work Stack M5 Windows Install and Durability Receipt

Date: 2026-08-30
Result: PASS for the bounded 64-bit Python 3.12 prototype installer

## Product coordinate

- Repository: `https://github.com/Shinick-Han/work-stack.git`
- Branch: `codex/workstack-ui-actions-20260830`
- Product/documentation commit: `ab40cb78ae42df77ce1342217c7ad3f2b480b9d4`
- Product/documentation tree: `97b9320635742fa2233f16256539fe365e7dd39e`
- Push: not performed

## Exact setup artifact

- Path: `.artifacts/WorkStack-Setup-1.0.0.ps1` (ignored build output)
- Bytes: `1,371,095`
- SHA-256: `e1b84dc32c8feee108d80c1b012f6dfd2cc8261a938b51bf69f99e235ce2de01`
- Target runtime: 64-bit Python 3.12
- Embedded dependency: hash-locked `unicodedata2==17.0.0` Windows AMD64 wheel

## Delivered behavior

- One PowerShell setup artifact extracts a bounded payload, creates an isolated virtual
  environment, installs only the hash-locked dependency, smoke-tests imports, and installs
  the production frontend.
- Application and planning data use separate LocalAppData roots.
- Launch creates a verified pre-launch backup and prunes only explicit older backup files.
- Upgrade stops only the exact installed Work Stack Python command, writes a pre-upgrade
  backup, stages the replacement, and retains rollback until installation succeeds.
- Uninstall preserves planning data by default.
- Maintenance backup holds the store lease and records an exact allowlisted file manifest,
  byte sizes, SHA-256 digests, workspace identity, product version, and store schema.
- Verify and restore reject tampering before destination creation. Existing-store restore
  requires a verified safety backup. Relocation copies and verifies without deleting source.

## Evidence

- Backend: 120 tests passed; one Windows symlink-privilege test skipped explicitly.
- Frontend: 24 files, 112 tests passed.
- Production build: passed; 912 modules; main JS 926.94 kB with the existing advisory.
- PowerShell AST parse: all six Windows scripts passed.
- Direct source install flow: install, first launch HTTP 200, stop, upgrade backup, reinstall,
  uninstall, and data-preserved assertion passed.
- Exact one-file artifact flow: SHA-256 above installed into a synthetic LocalAppData target,
  first launch returned HTTP 200, stop/uninstall removed the application, and workspace data
  remained present.
- Tampered archive, running-server lock, restore safety backup, round trip, and non-destructive
  relocation tests passed.
- Source export audit at product close: 185 UTF-8 files passed.
- Test installations were removed; synthetic preserved-data directories were moved to the
  Windows Recycle Bin after exact-target inspection.

## Boundaries and nonclaims

- The setup artifact is not code-signed or notarized and may show Windows execution warnings.
- Python 3.12 itself is not bundled; the current installer fails clearly on another runtime.
- No silent network updater, background update service, cloud backup, or Microsoft sync exists.
- Restore is an explicit offline command; there is no browser restore button while the server
  owns the planning store.
- Frozen Conduit contract, safety policy, conformance kit, canonical snapshot, disclosure UI,
  and file-only export behavior were not changed.
- No push, release upload, or remote clean-account runner evidence is claimed.
