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

export interface WorkspaceFilters {
  search?: string;
  status?: string;
  priority?: string;
  objectiveId?: string;
  readiness?: "all" | "ready" | "blocked";
  timing?: "all" | "overdue" | "today" | "soon" | "unscheduled";
  today?: string;
}

export interface WorkspaceViewsProps extends WorkspaceFilters {
  /** Active canvas. Graph and Treemap never mutate workspace data. */
  view: WorkspaceView;
  tasks: readonly WorkspaceTask[];
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
