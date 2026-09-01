# Work Stack Structural Quality Wave E1 Receipt

Date: 2026-08-31
Status: IMPLEMENTED_AND_LOCALLY_VERIFIED, UNCOMMITTED_AFTER_D6_CHECKPOINT

## Scope

Wave E1 split the frontend API monolith along two existing responsibilities without changing its public import surface:

- shared session, CSRF, retry, envelope, error, idempotency, and planning-change transport;
- verified backup and task-snapshot downloads;
- resource-specific Work Stack API methods remain assembled by `api/client.ts`.

## Repository coordinates

- Worktree: repository-local `.worktrees/source-providers` checkout; personal absolute prefix omitted.
- Branch: `codex/workstack-python-desktop-20260831`.
- Parent commit: `bc5d25da1018977672dc09732c503bb28c5a80f4`.
- Parent tree: `e91b98b933d8614fcf5a998b9f694ae15bcdeb00`.
- Git state: this Wave E1 packet is intentionally uncommitted while this receipt is written.

## Changed paths attributable to Wave E1

- `frontend/src/api/client.ts`
- `frontend/src/api/transport.ts`
- `frontend/src/api/transport.test.ts`
- `frontend/src/api/downloads.ts`
- `frontend/src/api/downloads.test.ts`
- this receipt

## RED-first contracts

The transport test was added before `transport.ts` existed and failed at module resolution. It then froze the following behavior:

- one restarted-server `403` refreshes the CSRF session once;
- the logical mutation body is byte-identical across the retry;
- the caller's idempotency key is identical across the retry;
- only the CSRF token changes.

The download tests were added before `downloads.ts` existed and failed at module resolution. They freeze:

- acceptance of a backup only with the expected content type, digest shape, workspace identity, and bounded filename;
- refusal of a backup from a different workspace;
- acceptance of a task snapshot only when its response digest and filename match the reviewed preview;
- refusal of a snapshot whose digest differs from the reviewed preview.

## Structural result

- `frontend/src/api/client.ts`: 651 lines before Wave E1, 416 lines after Wave E1.
- `frontend/src/api/transport.ts`: 162 lines.
- `frontend/src/api/downloads.ts`: 85 lines.
- Existing imports of `api`, `ApiError`, `CommitUnknownError`, and `createIdempotencyKey` from `api/client` remain valid.
- Structural quality gate: PASS, 84 production files.
- Candidate source digest: `7681ac3ee7a437ea50d2f66cf818ebcbe0f56ce41f1eca038bcf92691ca56b36`.
- Structural configuration digest: `eb24adf4b284f7e817e899b48bddb8dde89d5a04cf5743ef9d7a23670ce59083`.

## Verification evidence

- Targeted API tests: 13 passed across client, transport, and downloads.
- Full frontend suite with coverage: 48 files and 236 tests passed.
- Frontend aggregate statement/line coverage: 89.53%.
- `api/client.ts` line coverage: 77.23%, above its 77.00% proportional floor.
- `api/transport.ts` line coverage: 97.08%.
- `api/downloads.ts` line coverage: 82.71%.
- Proportional coverage gate: PASS with the existing noncritical `frontend/src/main.tsx` zero-line warning.
- TypeScript and production Vite build: PASS, 958 modules transformed.
- Build warnings are unchanged third-party Zod annotation and large-chunk advisories.
- Mutation sentinels: PASS, 3 killed and 0 survived.
- Export/privacy audit including this receipt: PASS, 351 UTF-8 source-policy files.
- `git diff --check`: PASS; only the standard LF-to-CRLF working-copy notice was emitted.

One concurrent PowerShell host-profile process emitted an `Import-Clixml` warning while the Git read-only inspection still completed. The warning did not originate in Work Stack and did not alter the working tree.

The full Python suite was not rerun because this packet changes only frontend TypeScript and tests. The pushed D6 parent passed 294 Python tests with one existing Windows symlink-privilege skip; Python critical mutation sentinels were rerun for this packet.

## Explicit nonclaims

- No user-visible workflow, route, API contract, persistence model, SSOT state, Microsoft session, SSH endpoint, Conduit boundary, or docking behavior changed.
- No release artifact, installer, update manifest, deployment, or publication was created.
- No browser screenshot or desktop-host smoke was required because the rendered UI is unchanged.
- This packet does not yet split `App.tsx` or `TaskDrawer.tsx`.
- This packet does not claim every API resource is independently modularized; it establishes and tests the shared transport and verified-download seams first.
