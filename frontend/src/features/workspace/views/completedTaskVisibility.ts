import {
  DONE_VISIBILITIES,
  type DoneVisibility,
} from "../../../domain/workspaceFilterTypes";
import type {
  CompletedVisibilityCounts,
  CompletedVisibilityInput,
  CompletedVisibilityProjection,
  CompletedVisibilityReveal,
  EmptyKind,
  PinReason,
  PrerequisiteClassification,
  PrerequisiteReason,
  WorkspaceFilters,
  WorkspaceTask,
  WorkspaceView,
} from "./types";

/**
 * Q3: the shared declarations moved to their owners - the filter coordinate to
 * the dependency-free domain module, the projector interfaces to views/types -
 * and are re-exported here unchanged, so every existing importer of this path
 * still gets the same constant instance and the same types.
 */
export { DONE_VISIBILITIES };
export type {
  CompletedVisibilityCounts,
  CompletedVisibilityInput,
  CompletedVisibilityProjection,
  CompletedVisibilityReveal,
  DoneVisibility,
  EmptyKind,
  PinReason,
  PrerequisiteClassification,
  PrerequisiteReason,
};
import { normalizeOutcomeFilter } from "./keyResultModel";
import { asTaskStatus, filterWorkspaceTasks } from "./viewModels";

/**
 * Presentation-only projection of completed Task visibility.
 *
 * Canonical Tasks, their statuses, parents, dependencies and revisions are never
 * altered here: the existing filter runs over the whole canonical array first, so
 * readiness still resolves completed prerequisites, and hiding is applied only to
 * the rendered set afterwards. The module is pure — no React, DOM, storage,
 * history, network, layout or clock access — and every date it needs arrives
 * through the caller's existing `WorkspaceFilters.today`.
 */

/** Display cap only; the unrevealed remainder is reported, never truncated away. */
export const REVEAL_DISPLAY_LIMIT = 10;

const PIN_ORDER: readonly PinReason[] = ["selected", "context", "focus"];

/** Mirrors the existing filter's `(value || "all").trim()` convention. */
function normalizedFilterValue(value: string | undefined): string {
  return (value || "all").trim();
}

/**
 * A supplied valid coordinate is kept, including an explicit `default`. Only an
 * omitted coordinate carrying a literal legacy `status=all` becomes `show`; null
 * and invalid values fall back to `default`.
 */
export function normalizeDoneVisibility(
  value: unknown,
  legacyExplicitAll = false,
): DoneVisibility {
  if (DONE_VISIBILITIES.includes(value as DoneVisibility)) {
    return value as DoneVisibility;
  }
  if (value === undefined && legacyExplicitAll === true) return "show";
  return "default";
}

/**
 * Identifies the filter/view scope a reveal request was made in, so a reveal
 * cannot survive into a different scope.
 */
/** Normalized outcome kind plus BOTH identifiers, so a parent change invalidates. */
function outcomeScopeSegments(outcome: WorkspaceFilters["outcome"]) {
  const normalized = normalizeOutcomeFilter(outcome ?? { kind: "all" });
  return normalized.kind === "pair"
    ? [normalized.kind, normalized.objectiveId, normalized.keyResultId]
    : [normalized.kind, null, null];
}

export function completedVisibilityScopeKey(
  view: WorkspaceView,
  filters: WorkspaceFilters,
  doneVisibility: DoneVisibility,
): string {
  return JSON.stringify([
    view,
    filters.search ?? "",
    filters.status ?? "all",
    filters.priority ?? "all",
    filters.objectiveId ?? "all",
    filters.readiness ?? "all",
    filters.timing ?? "all",
    ...outcomeScopeSegments(filters.outcome),
    filters.today ?? null,
    doneVisibility,
  ]);
}

function uniqueDependencyIds(task: WorkspaceTask): string[] {
  return [...new Set(task.dependencies ?? [])];
}

function resolvePins(
  input: CompletedVisibilityInput,
  canonicalById: ReadonlyMap<string, WorkspaceTask>,
) {
  const candidates: readonly (readonly [PinReason, string | null | undefined])[] = [
    ["selected", input.selectedTaskId],
    ["context", input.contextTargetTaskId],
    ["focus", input.focusPinnedTaskId],
  ];
  const reasonsById = new Map<string, PinReason[]>();
  const missing: string[] = [];

  for (const [reason, taskId] of candidates) {
    if (!taskId) continue;
    if (!canonicalById.has(taskId)) {
      if (!missing.includes(taskId)) missing.push(taskId);
      continue;
    }
    const existing = reasonsById.get(taskId);
    if (existing) existing.push(reason);
    else reasonsById.set(taskId, [reason]);
  }
  return { reasonsById, missing };
}

/**
 * Direct, canonical, Done prerequisites of the anchor that the current filters
 * already match and that only the Done rule is hiding. No parent or transitive
 * expansion, and another filter always beats Done.
 */
function eligibleRevealTargets(
  anchor: WorkspaceTask,
  canonicalById: ReadonlyMap<string, WorkspaceTask>,
  matchedIds: ReadonlySet<string>,
  visibleBeforeReveal: ReadonlySet<string>,
): string[] {
  return uniqueDependencyIds(anchor).filter((id) => {
    const dependency = canonicalById.get(id);
    if (!dependency) return false;
    if (asTaskStatus(dependency.status) !== "done") return false;
    if (!matchedIds.has(id)) return false;
    return !visibleBeforeReveal.has(id);
  });
}

function resolveReveal(
  input: CompletedVisibilityInput,
  scopeKey: string,
  canonicalById: ReadonlyMap<string, WorkspaceTask>,
  matchedIds: ReadonlySet<string>,
  visibleBeforeReveal: ReadonlySet<string>,
) {
  const request = input.reveal ?? null;
  const anchorId = input.prerequisiteAnchorTaskId ?? null;
  const empty = { reveal: null as CompletedVisibilityReveal | null, remaining: 0 };

  if (!request || !anchorId) return empty;
  if (request.scopeKey !== scopeKey || request.anchorTaskId !== anchorId) return empty;

  const anchor = canonicalById.get(anchorId);
  if (!anchor || !visibleBeforeReveal.has(anchorId)) return empty;

  const eligible = eligibleRevealTargets(
    anchor,
    canonicalById,
    matchedIds,
    visibleBeforeReveal,
  );
  const eligibleIds = new Set(eligible);
  const active: string[] = [];
  for (const id of request.taskIds) {
    if (active.length >= REVEAL_DISPLAY_LIMIT) break;
    if (!eligibleIds.has(id) || active.includes(id)) continue;
    active.push(id);
  }
  return {
    reveal: { anchorTaskId: anchorId, scopeKey, taskIds: active },
    remaining: eligible.length - active.length,
  };
}

function classifyPrerequisites(
  task: WorkspaceTask,
  canonicalById: ReadonlyMap<string, WorkspaceTask>,
  matchedIds: ReadonlySet<string>,
  visibleIds: ReadonlySet<string>,
): PrerequisiteClassification[] {
  return uniqueDependencyIds(task).map((id) => {
    let reason: PrerequisiteReason;
    if (!canonicalById.has(id)) reason = "missing";
    else if (visibleIds.has(id)) reason = "visible";
    else if (!matchedIds.has(id)) reason = "other-filter";
    else reason = "completed";
    return { id, reason, revealable: reason === "completed" };
  });
}

function emptyKindFor(
  visibleCount: number,
  canonicalTotal: number,
  filterMatchedTotal: number,
  hiddenCompleted: number,
): EmptyKind {
  if (visibleCount > 0) return "none";
  if (canonicalTotal === 0) return "no-tasks";
  if (filterMatchedTotal > 0 && hiddenCompleted === filterMatchedTotal) {
    return "all-complete";
  }
  return "other-filter-empty";
}

export function projectCompletedTaskVisibility(
  input: CompletedVisibilityInput,
): CompletedVisibilityProjection {
  const { tasks, filters } = input;
  const doneVisibility = input.doneVisibility ?? "default";

  // Canonical first: the existing filter must see completed prerequisites so
  // readiness, blockers and cycles keep resolving exactly as they do today.
  const matched = filterWorkspaceTasks(tasks, filters);
  const matchedIds = new Set(matched.map((task) => task.id));
  const canonicalById = new Map(tasks.map((task) => [task.id, task]));

  const hideDone =
    normalizedFilterValue(filters.status) !== "done" && doneVisibility !== "show";

  const baseVisibleIds = new Set(
    matched
      .filter((task) => !(hideDone && asTaskStatus(task.status) === "done"))
      .map((task) => task.id),
  );

  const { reasonsById: pinReasonsById, missing: missingPinTaskIds } = resolvePins(
    input,
    canonicalById,
  );

  const visibleBeforeReveal = new Set(baseVisibleIds);
  for (const taskId of pinReasonsById.keys()) visibleBeforeReveal.add(taskId);

  const scopeKey = completedVisibilityScopeKey(input.view, filters, doneVisibility);
  const { reveal, remaining } = resolveReveal(
    input,
    scopeKey,
    canonicalById,
    matchedIds,
    visibleBeforeReveal,
  );

  const visibleIds = new Set(visibleBeforeReveal);
  for (const taskId of reveal?.taskIds ?? []) visibleIds.add(taskId);

  // Canonical source order, original object references, no in-place sorting.
  const visibleTasks = tasks.filter((task) => visibleIds.has(task.id));
  const hiddenCompletedTaskIds: string[] = [];
  const hiddenOtherTaskIds: string[] = [];
  for (const task of tasks) {
    if (visibleIds.has(task.id)) continue;
    if (matchedIds.has(task.id)) hiddenCompletedTaskIds.push(task.id);
    else hiddenOtherTaskIds.push(task.id);
  }

  const retainedTaskIds = tasks
    .filter((task) => pinReasonsById.has(task.id) && !baseVisibleIds.has(task.id))
    .map((task) => task.id);

  const pinReasonsByTaskId: Record<string, readonly PinReason[]> = {};
  for (const task of tasks) {
    const reasons = pinReasonsById.get(task.id);
    if (!reasons) continue;
    pinReasonsByTaskId[task.id] = PIN_ORDER.filter((reason) => reasons.includes(reason));
  }

  const prerequisitesByTaskId: Record<string, readonly PrerequisiteClassification[]> = {};
  for (const task of visibleTasks) {
    prerequisitesByTaskId[task.id] = classifyPrerequisites(
      task,
      canonicalById,
      matchedIds,
      visibleIds,
    );
  }

  return {
    referenceTasks: tasks,
    visibleTasks,
    hiddenCompletedTaskIds,
    hiddenOtherTaskIds,
    retainedTaskIds,
    pinReasonsByTaskId,
    missingPinTaskIds,
    selection: {
      selectedTaskId: input.selectedTaskId ?? null,
      contextTargetTaskId: input.contextTargetTaskId ?? null,
      focusPinnedTaskId: input.focusPinnedTaskId ?? null,
    },
    reveal,
    revealRemainingCount: remaining,
    prerequisitesByTaskId,
    counts: {
      canonicalTotal: tasks.length,
      filterMatchedTotal: matched.length,
      visible: visibleTasks.length,
      hiddenCompleted: hiddenCompletedTaskIds.length,
      hiddenOther: hiddenOtherTaskIds.length,
      retained: retainedTaskIds.length,
      revealed: reveal?.taskIds.length ?? 0,
    },
    emptyKind: emptyKindFor(
      visibleTasks.length,
      tasks.length,
      matched.length,
      hiddenCompletedTaskIds.length,
    ),
  };
}
