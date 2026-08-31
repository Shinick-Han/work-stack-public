# Work Stack M18 First-Run Workspace Receipt

Date: 2026-08-30
Result: PASS

## Product coordinate

- Branch: `codex/workstack-ui-actions-20260830`
- Product commit: `84d116642623d138bc5ee9b0f4782f32c94e8fc9`
- Product tree: `d3a28064250bdfab7ed7117b2ade5e441f2829de`
- Push: not performed

## Behavior

- A store with zero Tasks displays `Start with an outcome—or capture the first task.` rather than
  claiming that filters hid existing work.
- `Define an objective` navigates to Objective Hub, where Objective creation already uses the
  idempotent v1 writer.
- `Create first task` opens the existing Quick Add dialog and its stable intent key flow.
- Objective creation remains optional. Copy states that a Task may be aligned now or later.
- View tabs, saved filters and visualization loading states are withheld until at least one Task
  exists. Populated workspaces with zero filtered matches retain the normal filter-empty state.

## Verification

- Fresh local production runtime on port 8772: first-run heading visible; workspace view tab count
  zero; Objective CTA reached `surface=objectives`; Task CTA opened the `New task` dialog.
- Frontend: 31 files / 134 tests passed.
- Browser: 11 existing seeded-workspace Playwright scenarios passed, preserving all mature flows
  and primary-surface axe checks.
- Production build: passed; initial JS 491.62 kB, CSS 87.25 kB.
- Backend/tooling: previously completed 134-test gate unchanged.
- Source audit after this receipt: 221 UTF-8 text files expected.
- Diff audit: passed before product commit.

## Boundaries and nonclaims

- No sample data is silently inserted. Demo seeding remains explicit.
- No Objective progress is inferred from Conduit or Task status.
- No Microsoft capability, Conduit client, watcher, transport, back-sync or planning-state export
  behavior changed. Frozen contract, safety-policy and conformance-kit bytes were not modified.
