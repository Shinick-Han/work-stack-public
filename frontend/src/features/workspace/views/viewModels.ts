import {
  TASK_PRIORITIES,
  TASK_STATUSES,
  type TaskPriority,
  type TaskStatus,
  type TreemapGroup,
  type WorkspaceEdge,
  type WorkspaceEdgeKind,
  type WorkspaceFilters,
  type WorkspaceNote,
  type WorkspaceObjective,
  type WorkspaceTask,
} from "./types";
import {
  blockingDependenciesFromIndex,
  indexDependencyTasks,
} from "../../tasks/taskRelationships";
import { dueReasonForDate } from "../../focus/focusModel";

export const STATUS_LABELS: Record<TaskStatus, string> = {
  open: "Open",
  started: "In progress",
  done: "Done",
  dropped: "Dropped",
};

export function asTaskStatus(value: string | undefined): TaskStatus {
  return TASK_STATUSES.includes(value as TaskStatus)
    ? (value as TaskStatus)
    : "open";
}
export function asTaskPriority(value: string | undefined): TaskPriority {
  return TASK_PRIORITIES.includes(value as TaskPriority)
    ? (value as TaskPriority)
    : "P2";
}

export function taskSubtaskProgress(task: WorkspaceTask) {
  const total = task.subtasks?.length ?? 0;
  const done = task.subtasks?.filter((subtask) => subtask.status === "done").length ?? 0;
  return { done, total };
}

export function objectiveTitle(objective: WorkspaceObjective): string {
  return objective.objective || objective.title || objective.id;
}

export function noteTitle(note: WorkspaceNote): string {
  return note.text || note.title || note.id;
}

function normalizedFilter(value: string | undefined): string {
  return (value || "all").trim();
}

export function filterWorkspaceTasks(
  tasks: readonly WorkspaceTask[],
  filters: WorkspaceFilters,
): WorkspaceTask[] {
  const query = (filters.search || "").trim().toLocaleLowerCase();
  const status = normalizedFilter(filters.status);
  const priority = normalizedFilter(filters.priority);
  const objectiveId = normalizedFilter(filters.objectiveId);
  const readiness = normalizedFilter(filters.readiness);
  const timing = normalizedFilter(filters.timing);
  const dependencyIndex = readiness === "all" ? null : indexDependencyTasks(tasks);

  return tasks.filter((task) => {
    if (status !== "all" && asTaskStatus(task.status) !== status) return false;
    if (priority !== "all" && asTaskPriority(task.priority) !== priority) return false;
    if (
      objectiveId !== "all" &&
      !(task.objective_ids || []).includes(objectiveId)
    ) {
      return false;
    }
    if (readiness !== "all") {
      const taskStatus = asTaskStatus(task.status);
      if (taskStatus !== "open" && taskStatus !== "started") return false;
      const blocked = blockingDependenciesFromIndex(dependencyIndex!, task).length > 0;
      if (readiness === "blocked" && !blocked) return false;
      if (readiness === "ready" && blocked) return false;
    }
    if (timing !== "all") {
      const taskStatus = asTaskStatus(task.status);
      if (taskStatus !== "open" && taskStatus !== "started") return false;
      if (timing === "unscheduled") {
        if (task.due) return false;
      } else {
        if (!filters.today) return false;
        const dueReason = dueReasonForDate(task.due ?? null, filters.today);
        const expectedKind = timing === "today" ? "due_today" : timing === "soon" ? "due_soon" : timing;
        if (dueReason?.kind !== expectedKind) return false;
      }
    }

    if (!query) return true;
    const searchable = [
      task.id,
      task.title,
      task.detail || "",
      ...(task.tags || []),
      ...(task.objective_ids || []),
    ]
      .join(" ")
      .toLocaleLowerCase();
    return searchable.includes(query);
  });
}

export type CanonicalEdgeKind = Exclude<
  WorkspaceEdgeKind,
  "objective" | "note"
>;

export interface CanonicalWorkspaceEdge {
  id: string;
  source: string;
  target: string;
  kind: CanonicalEdgeKind;
}

export function canonicalEdgeKind(kind: string | undefined): CanonicalEdgeKind {
  if (kind === "objective") return "alignment";
  if (kind === "note") return "reference";
  if (
    kind === "alignment" ||
    kind === "dependency" ||
    kind === "parent" ||
    kind === "reference"
  ) {
    return kind;
  }
  return "reference";
}

function edgeKey(edge: Pick<CanonicalWorkspaceEdge, "source" | "target" | "kind">) {
  return `${edge.kind}:${edge.source}:${edge.target}`;
}

/**
 * Rebuilds missing fixture relationships and overlays server-provided edges. This
 * keeps all 30 demo tasks useful with both the legacy snapshot and API v1.
 */
export function buildWorkspaceEdges(
  tasks: readonly WorkspaceTask[],
  notes: readonly WorkspaceNote[],
  suppliedEdges: readonly WorkspaceEdge[],
): CanonicalWorkspaceEdge[] {
  const collected: CanonicalWorkspaceEdge[] = [];
  const add = (source: string, target: string, kind: CanonicalEdgeKind) => {
    if (!source || !target) return;
    collected.push({ id: edgeKey({ source, target, kind }), source, target, kind });
  };

  for (const task of tasks) {
    for (const objectiveId of task.objective_ids || []) {
      add(task.id, objectiveId, "alignment");
    }
    for (const dependencyId of task.dependencies || []) {
      add(task.id, dependencyId, "dependency");
    }
    if (task.parent_id) add(task.id, task.parent_id, "parent");
  }
  for (const note of notes) {
    for (const target of note.links || []) add(note.id, target, "reference");
  }
  for (const edge of suppliedEdges) {
    add(edge.source, edge.target, canonicalEdgeKind(edge.kind));
  }

  const unique = new Map(collected.map((edge) => [edgeKey(edge), edge]));
  return [...unique.values()].sort((left, right) => left.id.localeCompare(right.id));
}

export function buildTreemapGroups(
  tasks: readonly WorkspaceTask[],
  objectives: readonly WorkspaceObjective[],
): TreemapGroup[] {
  const knownObjectives = new Set(objectives.map((objective) => objective.id));
  const groups: TreemapGroup[] = objectives.map((objective) => ({
    name: `${objective.id} · ${objectiveTitle(objective)}`,
    objectiveId: objective.id,
    children: [],
  }));
  const unaligned: TreemapGroup = {
    name: "Unaligned / Operations",
    objectiveId: "none",
    children: [],
  };
  const multiAligned: TreemapGroup = {
    name: "Multiple objectives",
    objectiveId: "multiple",
    children: [],
  };
  const byId = new Map(groups.map((group) => [group.objectiveId, group]));

  for (const task of tasks) {
    const validObjectiveIds = [...new Set((task.objective_ids || []).filter((id) =>
      knownObjectives.has(id),
    ))].sort();
    const objectiveId = validObjectiveIds.length > 1
      ? "multiple"
      : validObjectiveIds[0] ?? "none";
    const group = objectiveId === "none"
      ? unaligned
      : objectiveId === "multiple"
        ? multiAligned
        : byId.get(objectiveId);
    if (!group) continue;
    group.children.push({
      name: task.title,
      size:
        1 +
        (task.subtasks || []).length +
        Math.min(Math.max(task.context_count || 0, 0), 5) * 0.2,
      taskId: task.id,
      priority: asTaskPriority(task.priority),
      status: asTaskStatus(task.status),
      objectiveId,
      objectiveIds: validObjectiveIds,
    });
  }

  if (multiAligned.children.length) groups.push(multiAligned);
  if (unaligned.children.length) groups.push(unaligned);
  for (const group of groups) {
    group.children.sort((left, right) => left.taskId.localeCompare(right.taskId));
  }
  return groups.filter((group) => group.children.length > 0);
}

export function tasksByStatus(
  tasks: readonly WorkspaceTask[],
): Record<TaskStatus, WorkspaceTask[]> {
  const result: Record<TaskStatus, WorkspaceTask[]> = {
    open: [],
    started: [],
    done: [],
    dropped: [],
  };
  for (const task of tasks) result[asTaskStatus(task.status)].push(task);
  for (const status of TASK_STATUSES) {
    result[status].sort((left, right) => {
      const priorityDelta =
        TASK_PRIORITIES.indexOf(asTaskPriority(left.priority)) -
        TASK_PRIORITIES.indexOf(asTaskPriority(right.priority));
      return priorityDelta || left.id.localeCompare(right.id);
    });
  }
  return result;
}
