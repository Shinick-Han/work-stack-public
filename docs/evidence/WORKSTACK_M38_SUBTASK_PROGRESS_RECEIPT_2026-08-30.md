# Work Stack M38 subtask progress receipt

Date: 2026-08-30

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `fb2648a0b3eb884510282fa2599c621118a2f2e1`
- Product tree: `2b3edbce704628fa4fd6a5fa09037734dc756068`
- Push: not performed

## Delivered behavior

- Board cards with subtasks show a compact `completed/total` progress marker in their planning
  metadata footer.
- Table adds a dedicated Steps column with the same progress projection.
- Only subtasks whose explicit planning status is `done` contribute to the completed count.
- Tasks without subtasks show no Board marker and an em dash in Table.
- Accessible labels state the Task ID and full `n of total done` meaning.
- The projection does not infer or mutate the parent Task status.

## Verification

- RED-first Board and Table tests failed before the progress projection existed.
- Focused Board/Table/model gate: 3 files / 18 tests passed.
- Full frontend gate: 32 files / 161 tests passed.
- Browser gate: all 19 Playwright scenarios passed, including Board and Table production fixture
  assertions and primary-surface serious/critical accessibility scans.
- Direct production-browser inspection on port 8770 observed `✓ 1/2` on the T-0001 Board card and
  `1/2` in the same Task's Table Steps cell.
- Production build passed: initial bundle 498.43 kB, Task Drawer 35.59 kB, CSS 91.35 kB.
- Backend behavior was unchanged; its most recent complete gate is 138 tests passed with one Windows
  symlink privilege case skipped.
- Source export audit: 245 UTF-8 text files after this receipt.
- `git diff --check`: passed.
- The original checkout's six protected user-owned dirty paths were not edited or staged.

## Docking and safety boundary

The frozen contract (`cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`),
safety-policy root (`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`),
and conformance-kit root (`sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`)
were not changed. Subtasks remain deliberately omitted from the frozen snapshot-v1 export exactly
as disclosed; this local UI projection does not reinterpret or add export fields.

No Conduit client, loopback transport, watcher, cloud relay, back-sync, bulk import, mutable link
table, execution inference, agent start, or Taskroom start was added.

## Nonclaims and remaining dependencies

- Subtask progress is not execution telemetry and does not prove external completion.
- Completing all subtasks does not automatically complete the parent Task.
- No Microsoft provider Gate 0 capability was enabled or claimed.
- No Conduit consumer implementation was performed.
- No remote branch was pushed.
