# Work Stack Product Maturity Execution Roadmap

Date: 2026-08-30
Status: ACTIVE
Baseline: `codex/workstack-ui-actions-20260830` at
`9e2c74d7c6064540786b6496b1e567e89dc89ae1`

## Non-negotiable docking boundary

- Contract Revision 4: 37,567 bytes,
  `cb0a41f250ad1a4bff287a8268cb49e805770cf94682647fbac0e89375102f70`.
- Safety Policy Revision 5 root:
  `sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`.
- Shared conformance-kit root:
  `sha256:a18627e56e49bf0f488a4e42962faeb559e8d9e99ec4c27e0b389c2a075ae2b9`.
- Work Stack is the sole planning-state authority. Conduit is the sole execution-state
  authority.
- Work Stack exports one immutable Task revision as exact canonical bytes only after
  explicit disclosure confirmation. Export is read-only and never contacts Conduit.
- No Work Stack Conduit client, watcher, relay, background sync, back-sync, bulk import,
  mutable link table, agent start, room creation, or execution-state inference is in
  scope.
- A normative defect stops only the affected docking lane and requires bilateral
  amendment. Product-local defects do not change frozen bytes.

## Continuous phase order

### M0 — truth and mutation safety

1. Reconcile current status, release, and user documentation with WS1–WS4 and the UI
   action checkpoint.
2. Add idempotent v1 Objective and graph-note creation.
3. Give Task note/subtask mutations an explicit commit-unknown recovery path.
4. Retire equivalent unsafe legacy browser writers only after compatibility tests pass.

Gate: response loss cannot create a duplicate or strand a committed mutation behind a
misleading retry; full tests, build, source/runtime audit, and diff audit pass.

Progress: COMPLETE. Versioned idempotent writers, commit-unknown recovery, and the
retirement of every equivalent legacy browser writer are covered by backend and frontend
regressions. See `docs/evidence/WORKSTACK_M0_MUTATION_SAFETY_RECEIPT_2026-08-30.md`
and the M49 receipt below.

### M1 — repeatable release evidence

Add repository CI, deterministic browser smoke tests, and automated accessibility
checks. Keep the existing Windows symlink privilege skip explicit rather than hiding it.

Gate: a clean checkout can install locked dependencies and reproduce backend, frontend,
build, privacy, browser, and accessibility gates.

Progress: COMPLETE for the repository gate implementation. The workflow installs locked
dependencies and runs backend, frontend, build, source-audit, Chromium browser, and axe
checks. A 2026-08-30 remote run passed through the first 22 browser scenarios but exposed
an axe-scan timeout in the final scenario; that reliability defect is explicitly assigned
to M52 rather than being represented as a green run.

### M2 — Daily Review

Expose check-in, per-Task Done/Next/Blocker entries, daily history, and deterministic
weekly roll-up through strict versioned APIs and one React surface.

Gate: one user can complete the planning → execution evidence → weekly review loop
without CLI use; every writer is revision/idempotency safe.

### M3 — Objective/KR Hub

Expose Objective detail, Key Result creation/progress, linked Task roll-up, and explicit
status changes. Do not infer KR progress from Conduit execution state.

Gate: objectives and KRs are fully usable from the product UI and remain Work Stack
planning facts.

Progress: COMPLETE. Objective title/quarter and KR description/target/progress/status are
revision-guarded editable planning facts. Activity records changed field names without
duplicating the edited free text.

### M4 — Microsoft evidence lanes

Validate and enable Outlook read first, then Teams read, then one provider reply at a
time. Use the existing authenticated OOB agent capability; never store OAuth tokens or
claim provider health. Each flag requires its own retained non-sensitive Gate 0 evidence.

Gate: the exact flagged artifact passes the release matrix; unverified lanes remain
disabled and no automatic send/retry exists.

### M5 — install and local durability

Create a one-click Windows install/launch path, explicit upgrade behavior, automatic
versioned backups, verified restore, and workspace relocation. Runtime data remains
outside the installation directory.

Gate: install, first launch, upgrade, backup, restore, and uninstall-with-data-preserved
are reproducible on a clean Windows account.

Progress: COMPLETE for the unsigned prototype. The running product creates an explicitly
confirmed, verified full-workspace backup download without mutating the store; restore remains
offline and fail-closed. The one-file setup now bundles a hash-verified official Python 3.12.10
runtime, so target machines require neither Python nor Node.js. OS signing remains release debt.

### M6 — retrieval and scale

Add unified task/objective/note/capture/activity search, Table/List view, saved filters,
cross-tab refresh, durable drafts, bounded undo, code splitting, and graph scale tests.

Progress: COMPLETE. Unified privacy-minimized search, sortable Table, local saved filters,
content-free cross-tab refresh, bounded planning drafts, append-only status Undo, lazy chunks,
1,000-Task graph scale evidence, and content-free readiness diagnostics were green at
`1dad3bc63e97acc0281444a96533af87f2cb6220`. Follow-through commit
`9ebfa8da72492422c18576262c46c01fb8e4b63b` adds a mutation-invalidated, process-local
allowlisted search index and bounds Command palette DOM output. Synthetic 10,000-Task warm
search improved from about 1.84 seconds to a 5ms median without changing the JSON SSOT.

Gate: larger synthetic workspaces remain responsive and concurrent local tabs never
silently overwrite a newer revision.

### M7 — Conduit consumer handoff

Prepare only the Work Stack evidence needed by the frozen contract and coordinate the
consumer-side Conduit import/taskroom work through bounded Conduit-owned packets.
Imported description remains display/review/storage content and never agent execution
input. Work Stack does not implement transport or back-sync in v1.

Gate: the frozen cross-product acceptance target is met by the real products and direct
user observation; Work Stack planning state remains byte-identical across export.

Progress: Work Stack producer handoff is READY. A real synthetic snapshot was delivered
twice with identical 503 bytes and unchanged store hashes; the consumer packet and exact
artifact coordinate are recorded in `WORKSTACK_M7_CONDUIT_CONSUMER_HANDOFF_2026-08-30.md`.
Conduit/Core implementation and P4 cross-product acceptance remain consumer-owned and are
not claimed by this repository.

### M8 — usability and continuity follow-through

Add Focus inline Start/Done with bounded Undo, complete Objective/KR text editing, and expose
content-free store readiness plus verified backup download in the product UI.

Progress: COMPLETE at `f26d73c9244136d9027ba268dd365473e8547e0e`.

Gate: common daily planning and continuity actions no longer require the CLI, while restore,
external side effects, and Conduit execution remain outside the live browser.

### M9 — bounded indexed retrieval

Replace repeated full-roster server scans with a process-local, privacy-allowlisted search
projection keyed by Store generation, while retaining local JSON as the planning SSOT.

Progress: COMPLETE at `9ebfa8da72492422c18576262c46c01fb8e4b63b`. A 10,000-Task regression
proves warm indexed retrieval and post-mutation invalidation; persistence and FTS migration remain
deferred until dogfood evidence justifies them.

### M10 — Objective Hub creation closure

Create an Objective from its natural Hub context through the existing idempotent v1 contract,
select the committed Objective, and keep retries bound to the same intent key.

Progress: COMPLETE at `fdc179dd07f848069a2b780ee94ddc22bb074da8`. The cumulative local gate is
126 backend tests with one explicit Windows symlink skip, 30 frontend files / 126 tests,
11 Playwright scenarios, production build, 208-file source audit, and clean diff audit.

Gate: an empty or populated Objective Hub supports direct outcome creation without routing through
generic workspace actions; no cross-product ownership or export boundary changes.

### M11 — Capture source-update draft safety

Distinguish a Capture workflow revision from a new sanitized source fingerprint. Automatically
refresh untouched source-derived fields, but preserve edited title/context/tags and require an
explicit keep-or-refresh choice when a truly newer source arrives. Keep priority, due date, and
Objective alignment under the user's planning control in either choice.

Progress: COMPLETE at `3ed1bc993eb651cf4742a44e0f59bfb83aff9a6d`. The cumulative gate is
126 backend tests with one explicit Windows symlink skip, 30 frontend files / 128 tests,
11 Playwright scenarios, production build, 209-file source audit, and clean diff audit.

Gate: background Capture refresh cannot silently overwrite a source-based Task draft, and ordinary
link/dismiss/status revisions with an unchanged fingerprint do not create a false conflict.

### M12 — bounded cross-tab field rebase

Publish content-free refresh signals only after confirmed mutations, de-duplicate the same nonce
across BroadcastChannel and storage delivery, and automatically rebase one Task save only when all
locally pending fields are unchanged on the authoritative newer revision.

Progress: COMPLETE at `54204d7fe54e17e0967580e8c293a2dfdcd8b1b2`. The cumulative gate is
126 backend tests with one explicit Windows symlink skip, 30 frontend files / 130 tests,
11 Playwright scenarios plus three isolated cross-tab reruns, production build, 210-file source
audit, and clean diff audit.

Gate: reads never trigger cross-tab feedback loops; disjoint edits converge on monotonic revisions;
same-field conflicts and a second consecutive race still stop for explicit review.

### M13 — stale Workspace continuity

Retain and render the last confirmed Workspace when a background refresh fails. Surface the exact
failure and an explicit retry as a non-blocking warning, while preserving the full error state when
no usable Workspace has ever loaded.

Progress: COMPLETE at `6e4900f79d894f7964659b92b245487e44cacac7`. The cumulative gate is
126 backend tests with one explicit Windows symlink skip, 30 frontend files / 130 tests,
11 Playwright scenarios, production build, 211-file source audit, and clean diff audit.

Gate: a transient background read failure cannot replace usable confirmed planning state with an
error screen; no stale state is represented as freshly confirmed.

### M14 — large Graph viewport rendering

Keep the complete deterministic graph model and relationship set, but ask React Flow to mount only
visible nodes and edges once the graph exceeds 250 nodes. Preserve the existing small-graph DOM,
selection semantics, fit view, controls, and minimap.

Progress: COMPLETE at `0da585e3baa2138cb15f8ca796846686296affb3`. The cumulative gate is
126 backend tests with one explicit Windows symlink skip, 30 frontend files / 131 tests,
11 Playwright scenarios, production build, 212-file source audit, and clean diff audit.

Gate: the 1,020-node synthetic projection remains deterministic and bounded while large visual
surfaces use the library's viewport renderer; no graph data is discarded or persisted elsewhere.

### M15 — Gate 0-aware Capture trust

Keep generic context import manual-only and route OOB-shaped results through the provider-gated
Microsoft agent-result lane. Treat stored OOB provenance as supplied metadata until the exact
provider's read capability has retained Gate 0 evidence.

Progress: COMPLETE at `87e567059dab75b8e683c6ab1adb27c3202b1351`. The cumulative gate is
126 backend tests with one explicit Windows symlink skip, 30 frontend files / 133 tests,
11 Playwright scenarios, production build, 214-file source audit, and clean diff audit.

Gate: no generic/manual UI path or stored self-assertion can produce an `OOB verified` badge while
the relevant provider Gate is false; provider enablement remains evidence-controlled.

### M16 — self-contained Windows setup

Bundle the official 64-bit Python 3.12.10 embeddable runtime behind its pinned SHA-256, install
Unicode 17 dependencies into that isolated runtime at build time, and make install/launch/update/
stop/maintenance independent of host Python. Preserve the configured data directory and port on
explicit setup-artifact update, retain pre-upgrade backup and rollback, and keep uninstall data-
preserving by default.

Progress: COMPLETE at `47e9cdab11608466514b31f8a12c91b2ace43ee4`. The 16,882,786-byte
setup artifact installed and first-launched with host Python removed from `PATH`, reported API
ready, schema 3, product 1.0.0 and Unicode 17.0.0, preserved a custom data directory and port
through the explicit updater, created one pre-upgrade backup, preserved planning data on normal
uninstall, and removed only the isolated synthetic state during cleanup.

Gate: the target account needs no Python or Node.js install; setup performs no dependency download;
the bundled runtime and Unicode database are verified before installation is swapped.

### M17 — reproducible optional QR transfer tools

Keep SQR1 repository-transfer tooling outside the installed product while declaring its Windows
image dependencies with exact hashes, allowing dependency-free help, and refusing recursive
replacement of an existing output directory.

Progress: COMPLETE at `cbf5f7184b02ffef52918616db4097843872a8e6`. A locked temporary
tool environment packaged the frozen contract directory into a deterministic 47,081-byte archive,
rendered 40 QR PNG frames plus a contact sheet, decoded them, and restored exact SHA-256
`6d50e3d7d8001ee52070dfc0e40b6d8a6f8d846c272c18eb4c98d07a6921a3cb`.

Gate: `render_qr.py --help` and `restore_from_png.py --help` succeed without optional imports;
full PNG round-trip preserves exact bytes; non-empty output directories remain untouched.

### M18 — outcome-first empty Workspace

Distinguish a truly empty planning store from a populated workspace whose active filters return no
Tasks. Replace misleading filter-recovery text on first launch with direct Objective and Task
entry points while keeping Objective creation optional.

Progress: COMPLETE at `84d116642623d138bc5ee9b0f4782f32c94e8fc9`. A fresh production
runtime displayed no irrelevant view/filter controls, opened Objective Hub from `Define an
objective`, and opened the existing idempotent Quick Add dialog from `Create first task`.

Gate: first launch has no dead end or false statement about hidden Tasks; both start paths reuse
existing versioned, revision/idempotency-safe product flows.

### M19 — Objective-context Task creation and draft ownership

Open Quick Add from the selected Objective's linked-Task panel with that Objective preselected,
without discarding unrelated title, detail, priority, tag, or due-date draft fields. Make the
confirmed Task-create boundary the sole automatic owner of Quick Add draft deletion; unrelated
Objective and Graph-note mutations must not clear it.

Progress: COMPLETE at `19181bb4712ae90eb7be03af3814a176b0062b46`. The cumulative local gate is
134 backend tests with one explicit Windows symlink skip, 31 frontend files / 137 tests,
12 Playwright scenarios, production build, 222-file source audit after this receipt, and clean
diff audit.

Gate: an Objective can create a pre-aligned Task through the existing idempotent v1 API; confirmed
Task success clears the bounded local draft, commit-unknown preserves uncertainty, and unrelated
planning writers cannot erase the draft.

### M20 — Task-to-Objective reverse navigation

Turn aligned Objective badges in Task detail into explicit, accessible navigation. Close the Task
selection and open the exact Objective Hub record without writing planning state. Keep the control
unavailable while a Task save is in flight.

Progress: COMPLETE at `c8b67c18f091ede813d5136f26d91499e9fee487`. The cumulative local gate is
31 frontend files / 137 tests, 13 Playwright scenarios, production build, 223-file source audit
after this receipt, and clean diff audit.

Gate: Objective-to-Task creation and Task-to-Objective inspection form a navigable planning loop;
the reverse edge is URL-only navigation and does not add a writer or alter docking semantics.

### M21 — in-context Task relationship traversal

Project the current Task's resolvable parent and dependencies into a compact relationship summary
inside the Drawer. Open an exact related Task without editing either side, and omit broken targets
from the actionable set rather than producing a dead control.

Progress: COMPLETE at `d1817f5d0110214f5cd97a529662edd98825c330`. The cumulative local gate is
31 frontend files / 138 tests, 14 Playwright scenarios, production build, 224-file source audit
after this receipt, and clean diff audit.

Gate: graph relationships are directly traversable from detail without search; navigation remains
read-only and does not reinterpret parent/dependency data or change snapshot-v1 omissions.

### M22 — Windows setup transfer-integrity evidence

Emit a portable SHA-256 sidecar beside every one-file Windows setup artifact and provide a strict
offline verifier that binds the digest to the selected filename. Document the distinction between
transfer integrity and publisher authentication.

Progress: COMPLETE at `5dc0ce63b0f80f974af3bad87ee9408795c96ab5`. A rebuilt 17,377,431-byte
setup produced a 92-byte sidecar and verified digest
`5c74398160951af2544529e6cb46e45f42e26b8ae24e36ebd0341c27b8fb5184`; deliberate hash and
filename mismatches were both rejected. The cumulative backend/tool gate is 136 tests with one
explicit Windows symlink privilege skip and a 226-file source audit after this receipt.

Gate: a user can verify exact downloaded setup bytes before execution; the product does not claim
authentic publisher identity until an external trusted code-signing certificate is available.

### M23 — explicit unsaved Task exit boundary

Keep a failed or invalid Task edit visible in its owning Drawer. Prevent close, relationship or
Objective navigation, snapshot export, and secondary Task actions until the user either retries the
save or explicitly discards the local intent and restores the last confirmed Task.

Progress: COMPLETE at `43701f140191bcc071a43be9ed7076760c0633e3`. The cumulative local gate is
31 frontend files / 139 tests, 15 Playwright scenarios, production build, 227-file source audit
after this receipt, and clean diff audit.

Gate: a failed blur save cannot disappear behind navigation; discard is explicit and restores the
confirmed revision, while retry retains the existing revision/conflict rules.

### M24 — guarded browser navigation for Task drafts

Extend the Task edit lock beyond Drawer-owned controls. Reject programmatic Task replacement and
SPA history traversal while unsaved intent exists, restore the current canonical URL, and register
the native browser unload warning for reload, tab close, and cross-document navigation.

Progress: COMPLETE at `582c19a56fbcbd7e3c2b58695381a5c7aea0177f`. The cumulative local gate is
31 frontend files / 141 tests, 15 Playwright scenarios, production build, 228-file source audit
after this receipt, and clean diff audit.

Gate: browser Back and alternate Task selection cannot silently unmount the draft owner; the lock
is removed only after confirmed save or explicit discard.

### M25 — fail-closed Windows update checksum gate

Require the installed updater to run the strict setup filename/digest verifier before reading the
installed configuration or invoking downloaded setup code. Preserve the configured data directory
and port only after verification succeeds.

Progress: COMPLETE at `98593b562405805a2789390612c9f786bf81514d`. A disposable updater smoke
passed the configured data-directory/port values into a verified setup and rejected changed setup
bytes before the invocation marker existed. The cumulative backend/tool gate remains 136 tests with
one explicit Windows symlink privilege skip and a 229-file source audit after this receipt.

Gate: normal update cannot execute a setup artifact whose adjacent sidecar is missing, malformed,
names another artifact, or has a different digest; publisher signing remains an external lane.

### M26 — bidirectional Task relationship traversal

Complete the detail relationship projection in both directions. In addition to a Task's parent and
dependencies, show resolvable children and dependents and open the exact related Task without a
planning mutation. Broken or absent reverse targets remain non-actionable.

Progress: COMPLETE at `9f1d55ccd68a6b72563979d94af1487ac73351c9`. The cumulative local gate is
31 frontend files / 141 tests, 15 Playwright scenarios, production build, and a 230-file source
audit after this receipt.

Gate: every stored parent/dependency edge can be traversed from either endpoint in Task detail;
the projection adds no writer, persists no reverse index, and does not change snapshot-v1 or the
frozen docking contract.

### M27 — acyclic Task relationship integrity

Reject a parent or dependency edit when following that same relationship kind would return to the
edited Task. Evaluate the proposed edge inside the existing revision-guarded transaction and leave
all store bytes and the Task revision unchanged on refusal.

Progress: COMPLETE at `8b932cb4461f236c2a7ac4dcc7116498ac8d825b`. The cumulative gate is
138 backend tests with one explicit Windows symlink privilege skip, 31 frontend files / 141 tests,
16 Playwright scenarios, production build, and a 232-file source audit after this receipt.

Gate: direct and transitive parent/dependency cycles fail closed, acyclic rewiring remains valid,
and the product UI exposes the refusal and explicit discard without modifying docking semantics.

### M28 — guided safe relationship editing

Project the M27 cycle rule into the Task Drawer before submission. Remove cyclic candidates from
parent and dependency choices, replace free-form dependency IDs with existing-Task selection and
removable chips, and retain the server validator as the final authority.

Progress: COMPLETE at `1be5f1ce27dc90fa7851e802b42ef51aebe6565e`. The cumulative frontend
gate is 32 files / 143 tests, 16 Playwright scenarios, production build, and a 235-file source
audit after this receipt. The backend gate remains 138 tests with one explicit Windows symlink
privilege skip.

Gate: users cannot select a currently known cyclic candidate, can add/remove dependencies without
memorizing display IDs, and every change still uses the existing revision-guarded Task writer.

### M29 — bounded relationship candidate filtering

Replace per-candidate graph reconstruction with one reverse-edge traversal per relationship kind.
Derive the complete set of Tasks that can already reach the edited Task, then filter parent and
dependency choices by constant-time set membership.

Progress: COMPLETE at `1a18336621d390541af1e4745770a26c7155fa52`. The cumulative frontend
gate is 32 files / 145 tests, 16 Playwright scenarios, production build, and a 236-file source
audit after this receipt. A synthetic 10,000-Task parent chain completed inside the 500 ms gate;
the complete relationship test file finished in 29 ms during the full suite.

Gate: cyclic-candidate derivation is O(V+E) per relationship kind, preserves the M28 choices and
server safety boundary, and does not introduce a persisted reverse index.

### M30 — dependency-aware actionable Focus

Use existing dependency planning facts to distinguish an attention-worthy Task from one that can
actually start now. Keep blocked candidates visible after actionable candidates, identify the
unfinished prerequisite, and disable only the inline Start/Done action.

Progress: COMPLETE at `1a94bb8d2ec27f87933eec43fe5ef8a37df962b7`. The cumulative frontend
gate is 32 files / 148 tests, 16 Playwright scenarios, production build, and a 237-file source
audit after this receipt.

Gate: only `done` dependencies satisfy readiness; open, started, dropped, and missing dependencies
fail safe as blockers. Focus remains a read-only projection except for an explicit action on an
unblocked Task through the existing revision-guarded status writer.

### M31 — direct Focus blocker navigation

Turn each resolvable blocker badge into an explicit navigation control. Open the prerequisite Task
in the shared Drawer while preserving the Focus surface; leave missing dependency markers passive.

Progress: COMPLETE at `b929d3d0c969fd349de74db0b3b302fd432f5fae`. The cumulative frontend
gate remains 32 files / 148 tests and 16 Playwright scenarios, with a passing production build and
a 238-file source audit after this receipt.

Gate: a blocked candidate leads directly to the Task that can unblock it without any planning
mutation, and closing the Drawer returns to the same Focus projection.

### M32 — Objective execution readiness

Reuse the exact dependency-readiness projection in Objective Hub. Summarize each selected
Objective's linked Tasks as actionable, blocked, done, or dropped and identify unfinished
dependency IDs on blocked Task cards.

Progress: COMPLETE at `e35d94bf0e2712355fa55e9f7ba8ec8d086f809c`. The cumulative frontend
gate is 32 files / 150 tests, 17 Playwright scenarios, production build, and a 239-file source
audit after this receipt.

Gate: Focus and Objective Hub share one readiness rule; Objective cards remain navigational and
read-only, and no readiness count changes planning facts or the docking export.

### M33 — Workspace dependency readiness

Project the shared dependency-readiness rule into Board and Table without turning it into an
execution policy. Calculate against the complete Workspace even when the visible cards or rows are
filtered, identify every unfinished dependency, and open a resolvable prerequisite directly.

Progress: COMPLETE at `b670765ac9efc8e85682c4842812e4d86646f96f`. The cumulative frontend
gate is 32 files / 154 tests, 18 Playwright scenarios, production build, and a 240-file source
audit after this receipt.

Gate: filtered views cannot misclassify a hidden completed dependency; blocker navigation is
read-only, while Board/Table retain the user's explicit status controls for intentional parallel
work. No readiness projection changes planning facts or docking bytes.

### M34 — readiness filter and deep links

Let the user narrow the shared Workspace projection to active Tasks that are ready to act or
blocked by unfinished dependencies. Preserve the choice in canonical URL state and bounded saved
views, while migrating pre-readiness saved-view v1 records to `all` in memory.

Progress: COMPLETE at `f6165991456bde8cc2ac172a6de6fe7aa552802b`. The cumulative frontend
gate is 32 files / 157 tests, 19 Playwright scenarios, production build, and a 241-file source
audit after this receipt.

Gate: readiness filtering uses the complete Workspace dependency set, excludes done/dropped work
from execution-ready semantics, round-trips through deep links and saved views, and remains a
read-only projection over Work Stack planning facts.

### M35 — indexed dependency readiness

Remove the Task-by-Task full-roster map construction introduced by shared readiness projections.
Build one immutable ID index per Workspace snapshot and reuse it in Board, Focus, Objective Hub,
and Workspace readiness filtering.

Progress: COMPLETE at `07b5a2de0536666ef4d781335d79f81d0a33aad0`. The cumulative frontend
gate is 32 files / 158 tests, 19 Playwright scenarios, production build, and a 242-file source
audit after this receipt. The RED 10,000-Task readiness filter took about 5.02 seconds before the
change; the complete six-test model file finished in 29 ms in the final full suite.

Gate: readiness calculation is O(V+E) for one projection rather than O(V²), preserves exact blocker
semantics and UI behavior, and creates no persisted index or alternate planning authority.

### M36 — accurate filtered result totals

Use the same shared Workspace filter model for the visible-result summary as for the four canvas
views. Show `matched of total` whenever any search/status/priority/readiness/Objective filter is
active and retain the concise total when no filter is active.

Progress: COMPLETE at `6646be68e5eedb980cce5ff1315c9fde7012b5c6`. The cumulative frontend
gate is 32 files / 159 tests, 19 Playwright scenarios, production build, and a 243-file source
audit after this receipt.

Gate: the count and rendered view share one deterministic filter function, readiness remains
indexed, and the summary performs no planning mutation or cross-product communication.

### M37 — blocked-work summary signal

Replace the ambiguous P0-only `Needs focus` headline with the complete active blocked count derived
from the shared readiness model. Retain active P0 count as supporting context on the same metric.

Progress: COMPLETE at `801bdced8f6d163fd5bd1f4e09878620e34ffe38`. The cumulative frontend
gate remains 32 files / 159 tests and 19 Playwright scenarios, with a passing production build and
a 244-file source audit after this receipt.

Gate: the headline reflects dependency blockers across all active priorities, the P0 signal remains
visible, and both are read-only projections over the complete Workspace snapshot.

### M38 — subtask progress projection

Surface the existing subtask planning facts before opening Task actions. Show completed/total
progress on Board cards and in a dedicated Table column, while leaving Tasks without subtasks
uncluttered.

Progress: COMPLETE at `fb2648a0b3eb884510282fa2599c621118a2f2e1`. The cumulative frontend
gate is 32 files / 161 tests, 19 Playwright scenarios, production build, and a 245-file source
audit after this receipt.

Gate: only explicit `done` subtask status contributes to completion, the projection is accessible
and compact, and it introduces no writer or automatic parent-Task transition.

### M39 — Objective navigation from planning views

Turn aligned Objective IDs on Board and Table into explicit Objective Hub links and give Graph
Objective nodes the same navigation contract. Preserve Task selection semantics and keep the
transition read-only.

Progress: COMPLETE at `3435b0483be7c14ea650637b0b446b98c2ded8c4`. The cumulative frontend
gate is 32 files / 163 tests, 20 Playwright scenarios, production build, and a 246-file source
audit after this receipt.

Gate: Objective activation clears stale Task/Capture selection, persists the Objective in canonical
URL state, performs no planning mutation, and does not alter frozen docking bytes.

### M40 — Treemap Objective navigation closure

Complete Objective navigation across all four Workspace views with a compact Treemap Objective
navigator. Keep unaligned work informational and preserve Task-cell selection semantics.

Progress: COMPLETE at `4d301b6da716b097d854d6fd8d5c7d81801f2799`. The cumulative frontend
gate is 33 files / 165 tests, 21 Playwright scenarios, production build, and a 248-file source
audit after this receipt.

Gate: the visual control and accessible hit target are the same element, only real Objective IDs
are navigable, and the route transition remains read-only and docking-neutral.

### M41 — local-calendar due timing

Replace raw date arithmetic in the user's head with shared relative due meaning on Board and Table.
Reuse Focus's civil-calendar rules and keep dates as read-only planning facts.

Progress: COMPLETE at `d998bcb77d7ae65b06be3072c6a8d4eb0d16a02f`. The cumulative frontend
gate is 33 files / 167 tests, 21 Playwright scenarios, production build, and a 250-file source
audit after this receipt.

Gate: active urgency labels are DST-independent and deterministic, completed work is not mislabeled
overdue, and no scheduler, provider call, priority mutation, or docking change is introduced.

### M42 — independently removable filter chips

Turn active filter summaries into accessible controls that remove exactly one condition while
preserving the rest of the canonical URL and saved-view projection.

Progress: COMPLETE at `1c487accbe3e9b194737ef575b7d014097456ec6`. The cumulative frontend
gate is 33 files / 168 tests, 21 Playwright scenarios, production build without a chunk-size warning,
and a 251-file source audit after this receipt.

Gate: individual removal changes only its own URL field, the all-clear path remains available,
Table loads as a bounded lazy chunk, and no planning or docking state is changed.

### M43 — due timing deep links and saved views

Let the user select active work by overdue, due-today, due-soon, or unscheduled timing using the
same local-calendar rules as the visible labels. Persist timing only as URL/local preference state.

Progress: COMPLETE at `e5a063dcb48da27678b3056ffe40dd764bd765c2`. The cumulative frontend
gate is 33 files / 170 tests, 22 Playwright scenarios, production build without a chunk-size warning,
and a 252-file source audit after this receipt.

Gate: URL and saved-view state round-trip safely, legacy saved views default to all timing, completed
work is excluded from execution-timing buckets, and no Task, provider, or docking state is changed.

### M44 — nested keyboard action isolation

Prevent Enter/Space on a control inside a Board card or Table row from also activating its parent
Task selection. Keep pointer behavior and explicit parent-item keyboard selection intact.

Progress: COMPLETE at `f2b73ddb206addadd97040183f2eeb5b0585405a`. The cumulative frontend
gate is 33 files / 170 tests, 22 Playwright scenarios, production build without a chunk-size warning,
and a 253-file source audit after this receipt.

Gate: only the element owning focus handles the parent-item shortcut; nested controls retain their
own action and no planning, provider, or docking contract changes.

### M45 — Graph keyboard navigation

Give actionable Graph Task and Objective nodes one real keyboard focus target and route Enter/Space
through the existing shell navigation callbacks. Leave Note nodes informational.

Progress: COMPLETE at `c3d8f00a82c2cc1eb1322febc68ed90dce838ff5`. The cumulative frontend
gate is 34 files / 171 tests, 22 Playwright scenarios, production build without a chunk-size warning,
and a 255-file source audit after this receipt.

Gate: keyboard and pointer routes converge, Graph model output remains deterministic, and no
planning, provider, or docking state is changed.

### M46 — complete local saved-view lifecycle

Turn local saved filters into named views with an explicit create, apply, update, rename, and
remove lifecycle. Retain the applied view identity while its coordinates are edited so Update
cannot silently create a duplicate or modify another view.

Progress: COMPLETE at `59d0a4d3d2995ab744c2a3e6df7f41ba2b25b4ed`. The cumulative frontend
gate is 34 files / 173 tests, 22 Playwright scenarios, production build without a chunk-size
warning, and a 256-file source audit after this receipt. WorkspacePage is now a bounded lazy chunk,
reducing initial JS from 500.68 kB to 438.79 kB.

Gate: names are trimmed and bounded, storage retains the existing strict 12-view limit, legacy v1
records remain readable, and saved-view operations change only browser-local preference state.
No planning fact, provider capability, or docking byte is changed.

### M47 — durable Table display preferences and narrow-screen priority

Persist the Table sort field, direction, and comfortable/compact row density as a strict local
display preference. On narrow screens collapse technical metadata columns while retaining Task,
status, priority, due timing, readiness, and Objective planning context.

Progress: COMPLETE at `ca92a3daba444d13c096e5211394c3530d5194c5`. The cumulative frontend
gate is 35 files / 176 tests, 23 Playwright scenarios, production build without a chunk-size
warning, and a 259-file source audit after this receipt. Initial JS remains 438.79 kB and the lazy
Table chunk is 5.15 kB.

Gate: malformed or expanded local records are removed and fall back to deterministic defaults;
reload restores the exact display choice; 390×844 browser evidence retains core columns and hides
only technical columns. Preferences never mutate planning facts or docking output.

### M48 — visible release identity and safe support summary

Expose the running product version, store schema, manual verified-update boundary, and a one-click
support summary in Local continuity. Build that summary from an explicit aggregate allowlist and
exclude workspace identity, planning content, local paths, recipients, and credentials.

Progress: COMPLETE at `3accd5446aeeb39ad0b2ffacb998ec4a600115ed`. The cumulative frontend
gate is 35 files / 177 tests, 23 Playwright scenarios, production build without a chunk-size
warning, and a 260-file source audit after this receipt. Initial JS remains bounded at 440.01 kB.

Gate: the visible version is read from the authoritative local storage-status API; copying the
summary is an explicit user action; the product does not download or run an update. The UI states
the existing sidecar verification and pre-upgrade backup sequence without claiming publisher
signing or changing any planning/docking behavior.

### M49 — retire remaining legacy browser mutation writers

Disable the residual unversioned Objective, worklog, note, and Task-status browser writers
after the established Origin/CSRF boundary, while retaining the already-disabled legacy Task
create route and every v1 writer.

Progress: COMPLETE at `514d5835fb86c6ed3f58fefa17a8b0244323d9a8`. All five recognized
legacy mutation routes return HTTP 410 after a valid browser boundary and leave Task,
Objective, worklog, and note state unchanged. The backend gate remains 138 tests with one
explicit Windows symlink privilege skip; the source audit passes 260 UTF-8 files.

Gate: invalid browser boundaries still fail before route retirement, valid legacy calls fail
closed without mutation, and the versioned product paths remain authoritative.

### M50 — repository-truth documentation reconciliation

Reconcile the roadmap, current status, and release checklist with the pushed branch state,
completed M0/M1 scope, retired legacy writers, current build/test counts, and the observed
remote axe timeout. Do not claim Microsoft Gate 0, publisher signing, or Conduit consumer work.

Progress: COMPLETE in the bounded documentation commit following M49. This reconciliation
records both passing local evidence and the non-green remote run instead of treating either as
proof of the other.

Gate: current documents name the actual branch state and open external/reliability limits
without changing product behavior or frozen docking bytes.

### M51 — guided offline Windows maintenance

Wrap the verified backup, verification, restore, and relocation primitives in a dedicated
Start-menu Maintenance window. Keep restoration offline, require explicit confirmation, create
a safety backup before replacement, preserve the relocation source, and switch configuration
only after the destination verifies.

Progress: COMPLETE at `1d3a555dcec35e24888eefba97de327ed7fded85`. A disposable installed-layout
smoke completed backup verification, restore to an empty destination, verified relocation,
post-success configuration switch, and source-preservation checks. The backend gate is now
140 tests with one explicit Windows symlink privilege skip; source audit passes 262 files before
this receipt.

Gate: nontechnical maintenance no longer requires copying CLI commands, destructive choices are
explicit, a running exact Work Stack process blocks unattended maintenance, and no web restore or
background data movement was introduced.

### M52 — reliable accessibility and bounded cross-browser gates

Split the aggregate eight-surface axe loop into independent timeout budgets, add forced-colors
and 200%-reflow-equivalent viewport checks, and run only two high-value navigation scenarios in
Firefox and WebKit. Keep the full product regression in Chromium.

Progress: COMPLETE at `05dc87c082ca72ce73aabea16c65d6246876c4bd` with fail-closed CI hardening at
`291e08678a1b986112304c0710153484d79800f9`. Strict GitHub Actions run `33304485683` at tested
aggregate commit `499ecc07d290a7fb54e17e438b797853f4e35a5b` passed every independent step. The first pushed
run proved 32 Chromium and four Firefox/WebKit scenarios but also revealed that a later successful
command could mask an earlier frontend failure inside one PowerShell step. Every install, test,
build, and browser command is now a separate Actions step. The first lazy Workspace assertion also
has a bounded five-second wait and passed three focused local repetitions, the full local suite,
and the strict remote suite.

Gate: each axe surface has its own timeout, core interactions run on three browser engines in CI,
forced-colors and reduced CSS viewport retain essential planning controls, and no later command can
mask an earlier non-zero exit. An unavailable local engine is recorded rather than skipped or
represented as a pass.

## Operating rule

Each phase is RED-first, uses bounded commits, records exact branch/commit/tree and gate
counts, and advances automatically when its machine gate is green. Soft findings enter
the debt register; security, planning integrity, external side effects, frozen-byte
drift, or product-ownership violations stop only the affected lane.
