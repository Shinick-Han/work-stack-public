import { keyResultKey } from "./keyResultModel";
import type {
  KeyResultNode,
  KeyResultProjection,
  OutcomePlacement,
  TaskRefResolution,
  UnresolvedReason,
} from "./keyResultModel";
import type { WorkspaceTask } from "./types";

/**
 * Pure presentation model for Slice B.
 *
 * It consumes the admitted KeyResultProjection plus the already-visible Task
 * subset and derives only presentation data. It never reruns filter, readiness,
 * status or progress logic, never mutates its inputs, and constructs no
 * canonical edge.
 */

export const DERIVED_TASK_KEY_RESULT = "derived.task-key-result";
export const DERIVED_KEY_RESULT_OBJECTIVE = "derived.key-result-objective";

export type DerivedEdgeKind =
  | typeof DERIVED_TASK_KEY_RESULT
  | typeof DERIVED_KEY_RESULT_OBJECTIVE;

export interface DerivedOutcomeEdge {
  id: string;
  kind: DerivedEdgeKind;
  source: string;
  target: string;
  /** Always true: these are presentation edges, never canonical relationships. */
  derived: true;
}

export interface OutcomeChip {
  /** Collision-safe identity: Task segment plus the admitted scoped pair key. */
  key: string;
  objectiveId: string;
  keyResultId: string;
  /** Display text only; never an identity. */
  label: string;
  resolved: boolean;
  text: string | null;
  /** The model's closed reason when unresolved, otherwise null. */
  reason: UnresolvedReason | null;
}

export interface TaskOutcomeChips {
  taskId: string;
  placement: OutcomePlacement;
  chips: OutcomeChip[];
}

export type OutcomeTreemapBucket =
  | "multiple-outcomes"
  | "unresolved-outcome"
  | "unassigned-outcome";

export interface OutcomeTreemapLeaf {
  nodeKind: "task";
  id: string;
  name: string;
  size: number;
}

export interface OutcomeTreemapGroup {
  nodeKind: "key-result" | "bucket";
  key: string;
  name: string;
  objectiveId: string | null;
  keyResultId: string | null;
  bucket: OutcomeTreemapBucket | null;
  children: OutcomeTreemapLeaf[];
}

export interface OutcomeTreemapObjective {
  nodeKind: "objective";
  key: string;
  name: string;
  objectiveId: string | null;
  children: OutcomeTreemapGroup[];
}

/** Collision-safe typed endpoint keys: every segment escapes the separator. */
function segment(value: string) {
  return value.replace(/\\/g, "\\\\").replace(/\|/g, "\\|");
}

export function taskEndpointKey(taskId: string) {
  return ["endpoint", "task", segment(taskId)].join("|");
}

export function keyResultEndpointKey(nodeKey: string) {
  return ["endpoint", "key-result", segment(nodeKey)].join("|");
}

export function objectiveEndpointKey(objectiveId: string) {
  return ["endpoint", "objective", segment(objectiveId)].join("|");
}

function derivedEdgeId(kind: DerivedEdgeKind, source: string, target: string) {
  return ["derived", segment(kind), segment(source), segment(target)].join("|");
}

function edge(kind: DerivedEdgeKind, source: string, target: string): DerivedOutcomeEdge {
  return { id: derivedEdgeId(kind, source, target), kind, source, target, derived: true };
}

/**
 * Derived Task -> KR edges for resolved refs of visible Tasks, and KR ->
 * Objective edges for every displayed KR. Nothing here touches workspace.edges.
 */
export function deriveOutcomeEdges(
  projection: KeyResultProjection,
  visibleTasks: readonly WorkspaceTask[],
): DerivedOutcomeEdge[] {
  const visible = new Set(visibleTasks.map((task) => task.id));
  const edges: DerivedOutcomeEdge[] = [];
  const seen = new Set<string>();
  for (const task of projection.tasks) {
    if (!visible.has(task.taskId)) continue;
    for (const key of task.resolvedKeys) {
      const node = projection.byKey[key];
      if (!node) continue;
      const derived = edge(
        DERIVED_TASK_KEY_RESULT,
        taskEndpointKey(task.taskId),
        keyResultEndpointKey(node.key),
      );
      if (seen.has(derived.id)) continue;
      seen.add(derived.id);
      edges.push(derived);
    }
  }
  for (const node of projection.keyResults) {
    const derived = edge(
      DERIVED_KEY_RESULT_OBJECTIVE,
      keyResultEndpointKey(node.key),
      objectiveEndpointKey(node.objectiveId),
    );
    if (seen.has(derived.id)) continue;
    seen.add(derived.id);
    edges.push(derived);
  }
  return edges;
}

function chipLabel(objectiveId: string, keyResultId: string) {
  return `${objectiveId} · ${keyResultId}`;
}

/** Per-Task chips: every stored ref survives, resolved or not. */
export function outcomeChipsForTask(
  projection: KeyResultProjection,
  taskId: string,
): TaskOutcomeChips {
  const entry = projection.tasks.find((item) => item.taskId === taskId);
  if (!entry) {
    return { taskId, placement: "unassigned-outcome", chips: [] };
  }
  const chips = entry.refs.map((ref: TaskRefResolution) => {
    const objectiveId = ref.ref.objective_id;
    const keyResultId = ref.ref.key_result_id;
    const node = projection.byKey[
      keyResultKey(projection.workspaceId, objectiveId, keyResultId)
    ];
    return {
      key: ["chip", segment(taskId), keyResultKey(projection.workspaceId, objectiveId, keyResultId)]
        .join("|"),
      objectiveId,
      keyResultId,
      label: chipLabel(objectiveId, keyResultId),
      resolved: Boolean(ref.resolved && node),
      text: ref.resolved && node ? node.text : null,
      reason: ref.resolved ? null : ref.reason,
    };
  });
  return { taskId, placement: entry.placement, chips };
}

const BUCKET_NAMES: Record<OutcomeTreemapBucket, string> = {
  "multiple-outcomes": "Multiple outcomes",
  "unresolved-outcome": "Unresolved outcome",
  "unassigned-outcome": "Unassigned outcome",
};

function bucketFor(placement: OutcomePlacement): OutcomeTreemapBucket | null {
  if (placement === "single-outcome") return null;
  return placement;
}

function objectiveGroup(
  groups: Map<string, OutcomeTreemapObjective>,
  objectiveId: string | null,
  name: string,
  workspaceId: string,
) {
  // A typed key, so a real Objective literally named __none__ can never merge
  // with the null-parent bucket.
  const key = objectiveId === null
    ? ["group", segment(workspaceId), "no-objective"].join("|")
    : ["group", segment(workspaceId), "objective", segment(objectiveId)].join("|");
  const existing = groups.get(key);
  if (existing) return existing;
  const created: OutcomeTreemapObjective = {
    nodeKind: "objective",
    key,
    name,
    objectiveId,
    children: [],
  };
  groups.set(key, created);
  return created;
}

function childGroup(parent: OutcomeTreemapObjective, group: OutcomeTreemapGroup) {
  const existing = parent.children.find((item) => item.key === group.key);
  if (existing) return existing;
  parent.children.push(group);
  return group;
}

/**
 * Objective -> single resolved KR -> Task, with one labelled bucket for
 * multiple, unresolved and unassigned placements. Each visible Task appears
 * exactly once and keeps the caller's existing size value.
 */
export function buildOutcomeTreemap(
  projection: KeyResultProjection,
  visibleTasks: readonly WorkspaceTask[],
  sizeOf: (task: WorkspaceTask) => number,
): OutcomeTreemapObjective[] {
  const byTaskId = new Map(projection.tasks.map((item) => [item.taskId, item]));
  const groups = new Map<string, OutcomeTreemapObjective>();
  for (const task of visibleTasks) {
    const entry = byTaskId.get(task.id);
    const placement = entry?.placement ?? "unassigned-outcome";
    const leaf: OutcomeTreemapLeaf = {
      nodeKind: "task",
      id: task.id,
      name: task.title,
      size: sizeOf(task),
    };
    const bucket = bucketFor(placement);
    if (!bucket && entry) {
      const node = projection.byKey[entry.resolvedKeys[0]];
      if (node) {
        const parent = objectiveGroup(
          groups, node.objectiveId, node.objectiveTitle, projection.workspaceId,
        );
        childGroup(parent, {
          nodeKind: "key-result",
          key: node.key,
          name: `${node.objectiveId} · ${node.keyResultId}`,
          objectiveId: node.objectiveId,
          keyResultId: node.keyResultId,
          bucket: null,
          children: [],
        }).children.push(leaf);
        continue;
      }
    }
    const resolvedBucket: OutcomeTreemapBucket = bucket ?? "unassigned-outcome";
    const alignment = (task.objective_ids ?? [])[0] ?? null;
    const parent = objectiveGroup(
      groups,
      resolvedBucket === "unassigned-outcome" ? alignment : null,
      resolvedBucket === "unassigned-outcome" ? (alignment ?? "Operations") : "Outcomes",
      projection.workspaceId,
    );
    childGroup(parent, {
      nodeKind: "bucket",
      key: `bucket|${resolvedBucket}|${parent.key}`,
      name: BUCKET_NAMES[resolvedBucket],
      objectiveId: null,
      keyResultId: null,
      bucket: resolvedBucket,
      children: [],
    }).children.push(leaf);
  }
  return [...groups.values()];
}

/** Catalog entries stay available even when a KR has no linked Task at all. */
export function keyResultCatalog(projection: KeyResultProjection): KeyResultNode[] {
  return [...projection.keyResults];
}
