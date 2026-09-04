import type { DoneVisibility } from "../../../domain/workspaceFilterTypes";

/**
 * Q3: the completed-visibility projector's declarations live with the other
 * Workspace view types instead of being imported back from the projector.
 * The projector re-exports them, so no consumer import has to change.
 */
/** Why a Task stayed visible despite the current filters or the Done rule. */
export type PinReason = "selected" | "context" | "focus";

export type PrerequisiteReason =
  | "visible"
  | "completed"
  | "other-filter"
  | "missing";

export type EmptyKind = "none" | "no-tasks" | "all-complete" | "other-filter-empty";

export interface CompletedVisibilityReveal {
  anchorTaskId: string;
  scopeKey: string;
  taskIds: readonly string[];
}

export interface CompletedVisibilityInput {
  tasks: readonly WorkspaceTask[];
  filters: WorkspaceFilters;
  view: WorkspaceView;
  doneVisibility?: DoneVisibility;
  selectedTaskId?: string | null;
  contextTargetTaskId?: string | null;
  focusPinnedTaskId?: string | null;
  prerequisiteAnchorTaskId?: string | null;
  reveal?: CompletedVisibilityReveal | null;
}

export interface PrerequisiteClassification {
  id: string;
  reason: PrerequisiteReason;
  revealable: boolean;
}

export interface CompletedVisibilityCounts {
  canonicalTotal: number;
  filterMatchedTotal: number;
  visible: number;
  hiddenCompleted: number;
  hiddenOther: number;
  retained: number;
  revealed: number;
}

export interface CompletedVisibilityProjection {
  referenceTasks: readonly WorkspaceTask[];
  visibleTasks: readonly WorkspaceTask[];
  hiddenCompletedTaskIds: readonly string[];
  hiddenOtherTaskIds: readonly string[];
  retainedTaskIds: readonly string[];
  pinReasonsByTaskId: Readonly<Record<string, readonly PinReason[]>>;
  missingPinTaskIds: readonly string[];
  selection: {
    selectedTaskId: string | null;
    contextTargetTaskId: string | null;
    focusPinnedTaskId: string | null;
  };
  reveal: CompletedVisibilityReveal | null;
  revealRemainingCount: number;
  prerequisitesByTaskId: Readonly<Record<string, readonly PrerequisiteClassification[]>>;
  counts: CompletedVisibilityCounts;
  emptyKind: EmptyKind;
}

export const TASK_STATUSES = ["open", "started", "done", "dropped"] as const;
export const TASK_PRIORITIES = ["P0", "P1", "P2", "P3"] as const;

export type TaskStatus = (typeof TASK_STATUSES)[number];
export type TaskPriority = (typeof TASK_PRIORITIES)[number];
export type WorkspaceView = "graph" | "treemap" | "board" | "table";

export interface WorkspaceSubtask {
  id: string;
  title?: string;
  status?: string;
  priority?: string;
}

/**
 * The visualization layer deliberately accepts the legacy JSON projection as well
 * as the v1 API projection. Fields it does not render may remain on the object.
 */
export interface WorkspaceTask {
  id: string;
  title: string;
  detail?: string;
  status?: string;
  priority?: string;
  due?: string | null;
  tags?: readonly string[];
  objective_ids?: readonly string[];
  key_result_refs?: readonly { objective_id: string; key_result_id: string }[];
  parent_id?: string | null;
  dependencies?: readonly string[];
  subtasks?: readonly WorkspaceSubtask[];
  context_count?: number;
  revision?: number;
}

export interface WorkspaceObjective {
  id: string;
  objective?: string;
  title?: string;
  quarter?: string;
  status?: string;
  key_results?: readonly unknown[];
}

export interface WorkspaceNote {
  id: string;
  text?: string;
  title?: string;
  links?: readonly string[];
  created?: string;
}

export type WorkspaceEdgeKind =
  | "alignment"
  | "dependency"
  | "parent"
  | "reference"
  | "objective"
  | "note";

export interface WorkspaceEdge {
  id?: string;
  source: string;
  target: string;
  kind?: WorkspaceEdgeKind | string;
}

import type { ReactNode } from "react";



import type { KeyResultProjection, OutcomeFilter } from "./keyResultModel";

export interface WorkspaceFilters {
  search?: string;
  status?: string;
  priority?: string;
  objectiveId?: string;
  readiness?: "all" | "ready" | "blocked";
  timing?: "all" | "overdue" | "today" | "soon" | "unscheduled";
  /** Scoped outcome coordinate; composes by AND inside the same filter pass. */
  outcome?: OutcomeFilter;
  today?: string;
}

/**
 * Transient, view-local interaction identities carried through the coordinator.
 * They are never persisted to the URL, storage, Task fields or saved filters,
 * and DOM trigger references stay inside GraphView.
 */
export interface WorkspaceInteractionPins {
  /**
   * Renders the popup-owned prerequisite controls INSIDE the Graph context
   * modal. The Page still owns the projection, anchor and reveal state; this
   * only carries its element to the one place a modal leaves reachable.
   */
  renderPopupPrerequisites?: (taskId: string) => ReactNode;
  /** The independently open popup target, distinct from the shell selection. */
  contextTargetTaskId?: string | null;
  /** The Task whose trigger currently owns focus restoration. */
  focusPinnedTaskId?: string | null;
  onContextTargetChange?: (taskId: string | null) => void;
  onFocusPinChange?: (taskId: string | null) => void;
}

export interface WorkspaceViewsProps extends WorkspaceFilters, WorkspaceInteractionPins {
  /** Active canvas. Graph and Treemap never mutate workspace data. */
  view: WorkspaceView;
  tasks: readonly WorkspaceTask[];
  /**
   * The single authoritative projection. When supplied, the coordinator renders
   * projection.visibleTasks and never runs a second filter of its own.
   */
  projection?: CompletedVisibilityProjection;
  /** Scoped outcome coordinate for the isolated fallback projection only. */
  outcome?: OutcomeFilter;
  /** The single Page-owned Key Result projection; renderers never rebuild it. */
  keyResultProjection?: KeyResultProjection;
  /** Narrow callback: choosing an outcome only changes that filter coordinate. */
  onSelectOutcome?: (selection: { objectiveId: string; keyResultId: string }) => void;
  objectives: readonly WorkspaceObjective[];
  notes: readonly WorkspaceNote[];
  edges: readonly WorkspaceEdge[];
  today: string;
  selectedTaskId?: string | null;
  selectedObjectiveId?: string | null;
  /** Opens/selects a task in the shell-owned detail drawer. */
  onSelectTask: (taskId: string) => void;
  /** Focuses an Objective inside the current Workspace view. */
  onSelectObjective: (objectiveId: string) => void;
  /** Rejecting the returned promise tells BoardView to roll back its optimistic move. */
  onChangeTaskStatus: (
    taskId: string,
    status: TaskStatus,
  ) => Promise<void>;
  className?: string;
}

/** Singular alias for shells that use the component name as the prop type name. */
export type WorkspaceViewProps = WorkspaceViewsProps;

export interface TreemapLeaf {
  [key: string]: unknown;
  name: string;
  size: number;
  taskId: string;
  priority: TaskPriority;
  status: TaskStatus;
  objectiveId: string;
  objectiveIds: readonly string[];
}

export interface TreemapGroup {
  [key: string]: unknown;
  name: string;
  objectiveId: string;
  children: TreemapLeaf[];
}
