# Work Stack M49 Legacy Writer Retirement Receipt

Date: 2026-08-30
Branch: `codex/workstack-ui-actions-20260830`
Product commit: `514d5835fb86c6ed3f58fefa17a8b0244323d9a8`
Product tree: `931242b99e806fe090eaaad5677c6da05dca4904`

## Outcome

PASS. The remaining recognized unversioned browser writers are retired. A valid browser
request to legacy Objective create, worklog create, note create, or Task status PATCH now
returns HTTP 410 `legacy_writer_disabled`. The previously retired legacy Task create route
continues to return HTTP 410 `legacy_task_writer_disabled`.

The existing Host, JSON, Origin, and CSRF checks remain ahead of the retirement response.
Invalid browser boundaries therefore continue to fail before route dispatch. Versioned v1
writers are unchanged and remain the product's only mutation surface.

## Changed product paths

- `workstack/server.py`
- `tests/test_api.py`

## RED-first and verification evidence

- The new retirement regression failed against the prior implementation with three HTTP 201
  responses and one HTTP 200 Task status mutation.
- Focused browser-boundary and retirement tests: 2 passed.
- Full backend gate: 138 passed, 1 skipped. The skip is the explicit Windows symlink privilege
  case and was not hidden or converted to a pass.
- Source export audit: 260 UTF-8 text files passed under source policy.
- `git diff --check`: passed before the product commit.

The regression snapshots the authoritative Task before the calls and verifies that the Task,
Task count, Objective list, worklog days, and notes remain unchanged afterward.

## Frozen docking coordinates preserved

- Contract Revision 4 SHA-256:
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`
- Safety Policy Revision 5 root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`
- Shared conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`

No docking contract, canonical export, Unicode 17 validator, or planning-authority behavior
changed.

## Remaining debt and nonclaims

- The current remote CI run at pre-M49 commit
  `a98d9c9ef759f38398a03142a88da23be1396045` passed backend, frontend, build, source audit,
  and 22 Chromium scenarios, then failed because the final axe scan exceeded its 30-second
  test timeout. M49 does not claim that remote run is green.
- Offline maintenance still has a command-line usability gap; M51 owns the wrapper work.
- Cross-browser, forced-colors, zoom, and axe timeout reliability remain M52 scope.
- Outlook and Teams Gate 0 remain unverified and all four provider build flags remain false.
- The Windows prototype is not publisher-signed.
- Work Stack does not implement a Conduit client, transport, watcher, relay, back-sync, import,
  agent start, or taskroom creation.

At the product commit, the task-scoped worktree was clean. The six protected user-owned paths
in the original checkout were not read for mutation, staged, reset, committed, or otherwise
altered by M49.
