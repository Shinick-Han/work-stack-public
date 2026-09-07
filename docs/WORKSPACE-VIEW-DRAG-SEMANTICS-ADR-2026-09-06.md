# ADR: Local drag semantics for Workspace views

Date: 2026-09-06

Status: accepted implementation contract for T-0047

Baseline: `a737ff2f1a56057c938db063f9781864611e66b5`

## Context and boundary

`WorkspacePage` owns the authoritative completed/filter projection and already has the stable
`workspace.workspace.id`. `WorkspaceViews` passes that projection to lazy Graph, Treemap, and Table
renderers. Graph currently runs ELK, keeps its canvas mounted across an empty projection, disables
React Flow node dragging, and fits only once. Treemap gives each SVG Task cell Enter/Space selection
and derives either Objective buckets or the typed Objective → Key Result/bucket hierarchy. Table
sorts the visible projection by a device-global v1 preference and is the only one of these three
views with a planning mutation control (`onChangeTaskStatus`). Board drag is intentionally outside
this ADR: it changes planning status through that callback and already uses a 6 CSS-pixel pointer
activation constraint plus a keyboard sensor.

The new gestures are **local presentation edits**. They do not call an HTTP client, a host bridge,
`onChangeTaskStatus`, `onSelectObjective`, or `onSelectOutcome`; do not change Task/Objectives,
alignment, Key Result refs, parent/dependency/reference edges, priority, status, or due date; and do
not enter saved filters, URL state, snapshots, backups, audit exports, SSE, or Work Stack Store
files. `localStorage` on the current browser profile is the only durable boundary. Selection and
navigation keep their existing callbacks and remain distinct from a move handle.

## Decision

### One bounded local store

Use one small, non-React adapter in `views/localViewState.ts`. It owns envelope parsing, byte and
identity bounds, safe storage access, revision stamping, per-view reset, and `storage` event
subscription. View-specific modules own their payload codecs and normalization; the shared adapter
must not know Graph coordinates or ordering rules.

Each view has a separate key, preventing a Graph write from overwriting Table or Treemap state:

```text
workstack:local-view:v1:<encodeURIComponent(workspaceId)>:<graph|treemap|table>
```

The stored JSON envelope has exactly these keys:

```ts
type LocalViewEnvelope<V extends "graph" | "treemap" | "table", D> = {
  schemaVersion: 1
  workspaceId: string
  view: V
  revision: number
  writtenAt: number
  writerId: string
  data: D
}
```

`workspaceId` is nonempty and at most 128 UTF-16 code units and must equal the requested workspace;
`writerId` is a per-tab UUID/string of at most 64 characters; `revision` is an integer from 1 through
`Number.MAX_SAFE_INTEGER`; and `writtenAt` is a finite, nonnegative epoch millisecond. The complete
serialized record is at most 262,144 UTF-16 code units. Payload IDs are nonempty strings of at most
256 code units. Arrays reject duplicates. Numbers must be finite. Objects reject unknown keys and
prototype-bearing/non-plain values after JSON parse.

The adapter contract is `createLocalViewStore({ workspaceId, view, defaultData, parseData })`,
returning `read()`, `update(mutator)`, `reset()`, and `subscribe(listener)`. `update` rereads the
latest valid record immediately before applying the mutator, increments its revision, stamps this
tab, validates/serializes, and performs one `localStorage.setItem`. Parse, security, disabled
storage, or quota failures fall back to in-memory defaults without breaking the interaction and
announce once, politely, that the layout could not be saved. At a saturated revision the adapter
removes that one key and writes revision 1; ordering never depends on revision. Invalid v1 data is
removed; a later schema uses a new `v2` key and an explicit migration, never permissive v1 parsing.

`localStorage` operation order is the conflict authority (last completed write wins), not clock
time. A `storage` event for the exact workspace/view key replaces local idle state; key removal is a
reset. During a gesture, external state is buffered. Cancel applies the buffered state; commit
rereads and rebases the local delta on it, then writes once. Thus Graph edits to different nodes
compose, while concurrent edits to the same node or the same ordering scope are last-commit-wins.
Table and Treemap reorder commits replace only the affected scope. Listeners are removed on
workspace/view unmount. No custom `BroadcastChannel` is needed.

### Graph: coordinates and viewport

Graph drag means moving one rendered presentation node in graph coordinates. It never changes an
edge endpoint or entity relation. A multi-Objective Task remains one Task node with all existing
alignment edges. Stored data is:

```ts
type GraphLocalViewData = {
  positions: Array<{ id: string; x: number; y: number; touchedAt: number }>
  viewport: { x: number; y: number; zoom: number } | null
}
```

Allow at most 4,096 positions. Clamp writes to `x/y` in `[-1_000_000, 1_000_000]`; reject invalid
reads. `touchedAt` is a finite nonnegative epoch millisecond. Viewport `x/y` has the same bounds and
`zoom` is in React Flow's current `[0.25, 1.7]` range. Before a write, remove IDs absent from the
full canonical presentation catalog (including currently filtered Tasks), then evict least recently
touched entries if still over the cap.

ELK remains the default for new/unpositioned nodes. After each completed ELK layout, saved positions
override matching nodes, including nodes returning after filtering. An edge whose source or target
has a manual coordinate drops its stale ELK route and uses the existing live smooth-step fallback;
other edges retain ELK routing. A valid saved viewport is applied after React Flow init and suppresses
initial `fitView`; otherwise current one-time fit behavior remains. Persist pan/zoom only on
`onMoveEnd`, and node position only on `onNodeDragStop`, never for animation frames.

The node's existing card action still selects/opens it. A separate `Move <kind> <id>` handle is the
only drag activator and nested context controls remain non-draggable. React Flow uses a 6 CSS-pixel
`nodeDragThreshold`. Below threshold, the handle only receives focus and neither selects nor writes.
Pointer cancel, Escape, workspace/view change, reset, unmount, or loss of pointer capture restores
the pre-gesture coordinate and writes nothing. Pointer-up after activation commits the clamped
coordinate, even if the pointer is outside the canvas while capture is held.

Keyboard on the move handle: Enter/Space enters move mode; unmodified arrows move 16 graph units;
Shift+Arrow moves 64; Enter/Space commits; Escape restores. Focus stays on the same handle. A polite
live region announces pickup, coordinates after movement, commit/cancel, external replacement, and
reset. `Reset graph layout` clears both positions and viewport and invokes `fitView` when nodes are
present (or arms one fit for the next nonempty projection).

### Treemap: sibling order, never hierarchy

Treemap drag changes reading/navigation order only. It has two explicit handles:

- A handle on a real Objective navigator item reorders real Objective items in the navigator and
  the matching top-level Recharts input. Synthetic Operations/Multiple/Outcomes buckets retain
  their deterministic positions after the ordered real Objectives and are never navigation targets.
- A Task-cell move handle reorders that Task only among leaves of its current terminal presentation
  group. A cross-group drop is invalid and cancels. Key Result/bucket groups cannot be dragged.

Stored data is:

```ts
type TreemapLocalViewData = {
  orders: Array<{ scope: string; ids: string[]; touchedAt: number }>
}
```

Allow 512 scopes, 2,048 IDs per scope, and 10,000 IDs total. Stable, typed scopes distinguish
`legacy/root`, a legacy Objective/Operations/Multiple leaf group, `outcome/root`, and an outcome
terminal group using its existing collision-safe `groupKey`; raw labels and reserved words are
never identities. Legacy and outcome orders do not leak into each other.

A Task aligned to several Objectives stays once in the already-derived Multiple bucket. A Task with
several Key Result refs stays once in the already-derived multiple-outcomes bucket. Moving either
cannot align, unalign, resolve, duplicate, or reparent it. Filtering projects a stored full order
onto visible IDs. A reorder replaces only the visible-ID slots in that scope, so hidden IDs retain
their relative positions; newly seen IDs append in the view model's deterministic order. Canonically
deleted identities are pruned when the full reference catalog proves deletion, not merely when a
filter hides them.

Pointer interaction uses a dedicated DnD handle and the existing 6 CSS-pixel `PointerSensor`
distance; only handles use `touch-action: none`, leaving cell scrolling/navigation intact. Below
threshold is focus only. No target, a different terminal group, pointer cancel, Escape, workspace/
view change, reset, or unmount cancels and writes nothing. Keyboard uses Enter/Space to pick up,
ArrowLeft/ArrowUp for previous, ArrowRight/ArrowDown for next, Home/End for first/last,
Enter/Space to drop, and Escape to restore. Focus follows the moved handle and a polite live region
announces position and invalid/cancelled drops. `Reset treemap order` removes every Treemap scope.

### Table: an explicit Manual sort

Table drag means changing a per-workspace total Task order. Extend `TableSortField` with `manual` and
add an explicit `Manual order` control. The default remains ID ascending. Column-header sorting sets
its named field/direction, disables move handles, and preserves the manual list. Returning to Manual
restores it. A disabled handle is labelled `Choose Manual order to reorder <id>`; drag never silently
changes sort mode. The caption and header `aria-sort` describe the active mode/direction.

The Table record replaces the current global preference write for new data:

```ts
type TableLocalViewData = {
  density: "comfortable" | "compact"
  sortField: "manual" | "id" | "title" | "status" | "priority" | "due"
  descending: boolean
  order: string[]
}
```

Allow 10,000 unique Task IDs. On the first read for a workspace with no Table record, validated
`workstack:table-preferences:v1` density/sort seeds the new record in memory; it is not deleted, so
another workspace can migrate. All subsequent writes use the per-workspace key. Manual order starts
from the full `referenceTasks` order, normalized with unseen canonical IDs appended and proven-deleted
IDs removed. Under filters, replace only visible-ID slots when reordering, preserving hidden Tasks'
relative positions. Changing filters never writes. Non-manual comparators use Task ID as a stable
tie-breaker and do not consult manual order.

Pointer drag is handle-only with a 6 CSS-pixel activation distance and `touch-action: none` on the
handle. Below threshold does not activate the row. No target, pointer cancel, Escape, workspace/view
change, reset, or unmount restores the old order and writes nothing. Keyboard uses the same pickup,
previous/next, Home/End, drop, and cancel keys as Treemap; focus follows the row handle and the live
region announces its 1-based visible position. `Reset table order` uses `update` to write an empty
`order` while preserving density and active sort; it does not call the adapter's whole-key reset. If
Manual is active, reset immediately shows canonical `referenceTasks` order. Status `<select>`,
Objective/KR buttons, row Enter/Space selection, and status mutation remain isolated from the
handle.

## Shared ordering algorithm

Given stored total order `S` and currently visible canonical IDs `V`, normalize `S` by deduplicating,
removing only proven-deleted IDs, and appending missing canonical IDs. For a visible reorder, compute
the requested visible sequence `V'`, then walk normalized `S`: replace each slot whose ID is in `V`
with the next ID from `V'`; leave every hidden slot untouched. This makes filter changes read-only,
preserves hidden relative order, and gives Treemap scopes and Table one testable rule.

## Acceptance matrix

| ID | Acceptance evidence |
| --- | --- |
| P1 | Pointer movement below 6 CSS px writes nothing and preserves selection/navigation; activated valid drop writes exactly once. |
| P2 | Escape, pointer cancel, invalid/no target, workspace/view change, reset during drag, and unmount restore the snapshot and write nothing for the cancelled gesture. |
| K1 | Every pointer reorder/move has the specified handle-only keyboard flow, focus continuity, and polite live announcements. |
| G1 | Graph reload restores typed-node coordinates and viewport per workspace; unseen nodes use ELK and filtered nodes retain coordinates. |
| G2 | Moving a multi-Objective Task changes one coordinate only, keeps all edges, and invalidates stale routes incident to it. |
| TR1 | Treemap root and leaf order affect navigator/Recharts input deterministically; cross-group movement cannot change hierarchy. |
| TR2 | Multiple-Objective/multiple-outcome Tasks remain one leaf in their derived bucket before and after reorder. |
| TA1 | Table handles work only in Manual; header sort hides/disables manual ordering without erasing it, and returning to Manual restores it. |
| F1 | Filtered IDs keep relative slots, newly seen IDs append deterministically, and only canonical deletion prunes state. Filter changes perform no write. |
| S1 | Wrong workspace/view, unknown keys, duplicates, nonfinite/out-of-range values, oversize arrays/records, malformed JSON, and unavailable storage fail closed. |
| C1 | Same-view cross-tab writes/reset update idle tabs; active gestures buffer external state and rebase/cancel as specified without listener leaks. |
| R1 | Graph/Treemap reset removes only its workspace/view key; Table reset rewrites only its order coordinate. Each announces completion, and other views, workspaces, Table density/sort, URL, and planning data are unchanged. |
| B1 | Tests spy on all planning callbacks and transport/fetch: local drag, keyboard move, persistence, cross-tab, and reset produce zero planning/API writes. |

## Non-overlapping implementation packets

Land the T-0048 shared spine first; T-0049 and T-0050 can be implemented in parallel against its
published API. After that ordering constraint, the packets have no shared file ownership.

### T-0048 — Graph and shared spine

Owned production files: `frontend/src/features/workspace/WorkspacePage.tsx`,
`frontend/src/features/workspace/views/WorkspaceViews.tsx`,
`frontend/src/features/workspace/views/types.ts`, `frontend/src/types/workspace-views.d.ts`,
`frontend/src/features/workspace/views/localViewState.ts` (new),
`frontend/src/features/workspace/views/graphViewState.ts` (new),
`frontend/src/features/workspace/views/GraphView.tsx`, and
`frontend/src/features/workspace/views/GraphView.drag.css` (new). This packet threads required
`workspaceId`, implements the shared adapter and Graph contract, and must not change Treemap/Table
implementation files.

Owned tests: `localViewState.test.ts` and `graphViewState.test.ts` (new beside the view modules),
`GraphView.drag.test.tsx` (new), plus Graph-specific additions to `GraphView.keyboard.test.tsx`,
`GraphView.scale.test.ts`, `graphLayout.test.ts`, and `WorkspaceViews.test.tsx`. Cover P1/P2/K1,
G1/G2, S1/C1/R1/B1, empty-overlay viewport survival, and workspace switching.

### T-0049 — Treemap order/navigation

Owned production files: `frontend/src/features/workspace/views/TreemapView.tsx`,
`frontend/src/features/workspace/views/treemapViewState.ts` (new),
`frontend/src/features/workspace/views/treemapOrdering.ts` (new), and
`frontend/src/features/workspace/views/TreemapView.drag.css` (new). It consumes the T-0048
`workspaceId` prop/store API and must not edit common, Graph, Table, or `viewModels.ts` files.

Owned tests: `TreemapView.test.tsx` plus new `treemapViewState.test.ts` and
`treemapOrdering.test.ts`. Cover P1/P2/K1, TR1/TR2, F1, S1/C1/R1/B1, legal Objective IDs named
`none`/`multiple`, Operations, duplicate raw KR IDs in distinct Objectives, and legacy/outcome scope
isolation.

### T-0050 — Table manual order

Owned production files: `frontend/src/features/workspace/views/TableView.tsx`,
`frontend/src/features/workspace/views/tablePreferences.ts`,
`frontend/src/features/workspace/views/tableOrdering.ts` (new), and
`frontend/src/features/workspace/views/TableView.drag.css` (new). It consumes the T-0048
`workspaceId` prop/store API and must not edit common, Graph, or Treemap files.

Owned tests: `TableView.test.tsx`, `tablePreferences.test.ts`, and new
`tableOrdering.test.ts`. Cover P1/P2/K1, TA1/F1, S1/C1/R1/B1, legacy preference seeding,
workspace isolation, stable comparator ties, status-control isolation, and 10,000-ID bounds.

Each packet runs its named Vitest files, `npm run build` in `frontend`, and repository
`git diff --check`. No packet installs dependencies, changes API/backend/Store code, pushes, merges,
or writes Work Stack SSOT.

## Rejected alternatives

- Persisting coordinates/order in planning Tasks or adding an API endpoint: creates a second meaning
  for planning fields and turns personal layout into shared SSOT.
- One envelope for every workspace/view: unrelated writers can lose each other's sections during
  concurrent read-modify-write and packets would share ownership.
- Dragging Treemap leaves between groups: visually suggests alignment/KR reassignment while bypassing
  its validation and mutation contract.
- Making Table drag silently switch from a named sort to Manual: a pointer accident would change the
  user's comparison mode and obscure why rows moved.
- Persisting every pointer frame or using timestamps to resolve conflict: increases storage churn and
  still cannot establish causality across tabs.
