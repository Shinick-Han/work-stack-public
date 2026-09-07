import { MarkerType } from "@xyflow/react";
import {
  GRAPH_MOVE_HANDLE_SELECTOR,
} from "./graphViewState";
import {
  GRAPH_NODE_SIZES,
} from "./graphLayout";
import type { KeyResultNode, KeyResultProjection, OutcomeFilter } from "./keyResultModel";
import {
  DERIVED_KEY_RESULT_OBJECTIVE,
  DERIVED_OBJECTIVE_TASK,
  DERIVED_TASK_KEY_RESULT,
  deriveOutcomeEdges,
  keyResultCatalog,
  keyResultEndpointKey,
  objectiveEndpointKey,
  taskEndpointKey,
  type DerivedOutcomeEdge,
} from "./keyResultViewModel";
import type {
  WorkspaceEdge,
  WorkspaceNote,
  WorkspaceObjective,
  WorkspaceTask,
} from "./types";
import {
  asTaskPriority,
  asTaskStatus,
  buildWorkspaceEdges,
  noteTitle,
  objectiveTitle,
  type CanonicalEdgeKind,
} from "./viewModels";
import type {
  GraphEntityKind,
  GraphKeyResultSelection,
  GraphNodeKind,
  WorkspaceFlowEdge,
  WorkspaceFlowNode,
} from "./graphViewTypes";
import { EDGE_COLORS } from "./graphViewTypes";

const OUTCOME_EDGE_COLORS = {
  [DERIVED_TASK_KEY_RESULT]: "var(--wsv-relation-outcome-task)",
  [DERIVED_KEY_RESULT_OBJECTIVE]: "var(--wsv-relation-outcome-objective)",
  [DERIVED_OBJECTIVE_TASK]: "var(--wsv-relation-outcome-task)",
} as const;

export const OUTCOME_LEGEND = [
  {
    key: DERIVED_KEY_RESULT_OBJECTIVE,
    label: "outcome",
    color: OUTCOME_EDGE_COLORS[DERIVED_KEY_RESULT_OBJECTIVE],
    accessibleName: "Outcome hierarchy: Objective to key result",
  },
  {
    key: DERIVED_TASK_KEY_RESULT,
    label: "outcome task",
    color: OUTCOME_EDGE_COLORS[DERIVED_TASK_KEY_RESULT],
    accessibleName: "Outcome hierarchy: key result to Task",
  },
  {
    key: DERIVED_OBJECTIVE_TASK,
    label: "outcome alignment",
    color: OUTCOME_EDGE_COLORS[DERIVED_OBJECTIVE_TASK],
    accessibleName: "Outcome hierarchy: Objective to Task",
  },
] as const;

const GRAPH_VIRTUALIZATION_THRESHOLD = 250;
const INITIAL_NODE_DIMENSIONS = {
  objective: { initialWidth: 221, initialHeight: 78 },
  task: { initialWidth: 216, initialHeight: 82 },
  note: { initialWidth: 232, initialHeight: 78 },
  // GN3: one policy. The first paint uses exactly the geometry graphLayout
  // hands ELK, so the frame never has to grow after the layout returns.
  "key-result": {
    initialWidth: GRAPH_NODE_SIZES["key-result"].width,
    initialHeight: GRAPH_NODE_SIZES["key-result"].height,
  },
} as const;

type LiveEntities = {
  tasks: ReadonlySet<string>;
  objectives: ReadonlySet<string>;
  notes: ReadonlySet<string>;
};

type VisibleGraphEntities = {
  sortedTasks: WorkspaceTask[];
  visibleObjectives: WorkspaceObjective[];
  visibleNotes: WorkspaceNote[];
  displayedKeyResults: KeyResultNode[];
  activeTaskSelection: string | null;
  activeObjectiveSelection: string | null;
  activeNoteSelection: string | null;
  graphKeyResultNode: KeyResultNode | undefined;
  taskIds: Set<string>;
  visibleObjectiveIds: Set<string>;
  visibleNoteIds: Set<string>;
};

function compareTasksByObjectiveThenId(left: WorkspaceTask, right: WorkspaceTask) {
  const objectiveDelta = (left.objective_ids?.[0] || "ZZZ").localeCompare(
    right.objective_ids?.[0] || "ZZZ",
  );
  return objectiveDelta || left.id.localeCompare(right.id);
}

function noteLinksVisibleWork(
  note: WorkspaceNote,
  taskIds: ReadonlySet<string>,
  visibleObjectiveIds: ReadonlySet<string>,
) {
  return (note.links || []).some(
    (id) => taskIds.has(id) || visibleObjectiveIds.has(id),
  );
}

function nodePosition(kind: GraphNodeKind, index: number, count: number) {
  if (kind === "objective") return { x: 24, y: 56 + index * 150 };
  if (kind === "note") return { x: 1_150, y: 56 + index * 140 };
  if (kind === "key-result") return { x: 186, y: 56 + index * 128 };

  const columns = count > 16 ? 3 : 2;
  const column = index % columns;
  const row = Math.floor(index / columns);
  return { x: 350 + column * 268, y: 34 + row * 118 };
}

/**
 * GR02: live Flow identity is a typed presentation value, not a canonical ID.
 * A schema-valid Objective ID may literally equal an escaped key-result
 * endpoint string, so every kind is namespaced and every segment escaped.
 * Canonical IDs, relationships and callback values are untouched.
 */
function flowNodeId(kind: GraphNodeKind, raw: string) {
  const escaped = raw.replace(/\\/g, "\\\\").replace(/\|/g, "\\|");
  return ["flow", kind, escaped].join("|");
}

/**
 * GC-F1: each relationship states which entity kinds its endpoints may be.
 * A dependency or parent endpoint is a Task and nothing else; an alignment
 * runs Task -> Objective.
 *
 * TE-F1: a Note reference is genuinely general. buildWorkspaceEdges emits one
 * reference per Note link with no restriction on the target's kind, so a link
 * to another already-live Note is as legitimate as one to a Task or Objective.
 * Its target keeps the ordered Task/Objective/Note lookup; this is the existing
 * precedence, not a new ambiguity rule or an unknown-kind fallback.
 */
function endpointKindsFor(kind: CanonicalEdgeKind): readonly [readonly GraphEntityKind[], readonly GraphEntityKind[]] {
  if (kind === "alignment") return [["task"], ["objective"]];
  if (kind === "reference") return [["note"], ["task", "objective", "note"]];
  return [["task"], ["task"]];
}

function presentationEndpoint(
  rawId: string,
  allowed: readonly GraphEntityKind[],
  live: LiveEntities,
) {
  for (const kind of allowed) {
    const known = kind === "task" ? live.tasks : kind === "objective" ? live.objectives : live.notes;
    if (known.has(rawId)) return flowNodeId(kind, rawId);
  }
  return null;
}

function relatedPresentationIds(
  selectionFlowId: string | null,
  edges: readonly { source: string; target: string }[],
) {
  const related = new Set<string>();
  if (!selectionFlowId) return related;
  related.add(selectionFlowId);
  for (const edge of edges) {
    if (edge.source === selectionFlowId) related.add(edge.target);
    if (edge.target === selectionFlowId) related.add(edge.source);
  }
  return related;
}

function outcomeEndpointFlowIds(
  visibleObjectives: readonly WorkspaceObjective[],
  sortedTasks: readonly WorkspaceTask[],
  keyResults: readonly KeyResultNode[],
) {
  const nodeIdForEndpoint = new Map<string, string>();
  for (const objective of visibleObjectives) {
    nodeIdForEndpoint.set(objectiveEndpointKey(objective.id), flowNodeId("objective", objective.id));
  }
  for (const task of sortedTasks) {
    nodeIdForEndpoint.set(taskEndpointKey(task.id), flowNodeId("task", task.id));
  }
  for (const node of keyResults) {
    nodeIdForEndpoint.set(keyResultEndpointKey(node.key), flowNodeId("key-result", node.key));
  }
  return nodeIdForEndpoint;
}

function outcomeFlowPairsFor(
  outcomeEdges: readonly DerivedOutcomeEdge[],
  visibleObjectives: readonly WorkspaceObjective[],
  sortedTasks: readonly WorkspaceTask[],
  keyResults: readonly KeyResultNode[],
) {
  const nodeIdForEndpoint = outcomeEndpointFlowIds(visibleObjectives, sortedTasks, keyResults);
  const pairs: { source: string; target: string }[] = [];
  for (const outcomeEdge of outcomeEdges) {
    const source = nodeIdForEndpoint.get(outcomeEdge.source);
    const target = nodeIdForEndpoint.get(outcomeEdge.target);
    if (!source || !target) continue;
    pairs.push({ source, target });
  }
  return pairs;
}

function buildOutcomeFlowEdges(
  outcomeEdges: readonly DerivedOutcomeEdge[],
  nodes: readonly WorkspaceFlowNode[],
  visibleObjectives: readonly WorkspaceObjective[],
  sortedTasks: readonly WorkspaceTask[],
  keyResults: readonly KeyResultNode[],
): WorkspaceFlowEdge[] {
  const liveNodeIds = new Set(nodes.map((node) => node.id));
  const nodeIdForEndpoint = outcomeEndpointFlowIds(visibleObjectives, sortedTasks, keyResults);
  const derived: WorkspaceFlowEdge[] = [];
  for (const outcomeEdge of outcomeEdges) {
    const source = nodeIdForEndpoint.get(outcomeEdge.source);
    const target = nodeIdForEndpoint.get(outcomeEdge.target);
    if (!source || !target) continue;
    if (!liveNodeIds.has(source) || !liveNodeIds.has(target)) continue;
    const color = OUTCOME_EDGE_COLORS[outcomeEdge.kind];
    derived.push({
      id: outcomeEdge.id,
      source,
      target,
      type: "planning",
      animated: false,
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 12, height: 12 },
      style: { stroke: color, strokeWidth: 1.2, strokeDasharray: "2 4", opacity: 0.6 },
      data: { kind: outcomeEdge.kind },
    });
  }
  return derived;
}

function matchingGraphKeyResult(
  displayedKeyResults: readonly KeyResultNode[],
  selectedGraphKeyResult: GraphKeyResultSelection | null,
) {
  if (!selectedGraphKeyResult) return undefined;
  return displayedKeyResults.find((node) =>
    node.objectiveId === selectedGraphKeyResult.objectiveId
    && node.keyResultId === selectedGraphKeyResult.keyResultId);
}

function visibleGraphEntities(
  tasks: readonly WorkspaceTask[],
  objectives: readonly WorkspaceObjective[],
  notes: readonly WorkspaceNote[],
  selectedTaskId: string | null | undefined,
  selectedObjectiveId: string | null | undefined,
  keyResultProjection: KeyResultProjection | null,
  selectedNoteId: string | null | undefined,
  selectedGraphKeyResult: GraphKeyResultSelection | null,
): VisibleGraphEntities {
  const sortedTasks = [...tasks].sort(compareTasksByObjectiveThenId);
  const taskIds = new Set(sortedTasks.map((task) => task.id));
  const activeTaskSelection =
    selectedTaskId && taskIds.has(selectedTaskId) ? selectedTaskId : null;
  const displayedKeyResults: KeyResultNode[] = keyResultProjection
    ? keyResultCatalog(keyResultProjection)
    : [];
  const objectiveIds = new Set([
    ...sortedTasks.flatMap((task) => [...(task.objective_ids || [])]),
    ...displayedKeyResults.map((node) => node.objectiveId),
  ]);
  const visibleObjectives = [...objectives]
    .filter((objective) => objectiveIds.has(objective.id))
    .sort((left, right) => left.id.localeCompare(right.id));
  const visibleObjectiveIds = new Set(visibleObjectives.map(({ id }) => id));
  const activeObjectiveSelection = selectedObjectiveId
    && visibleObjectiveIds.has(selectedObjectiveId)
    ? selectedObjectiveId
    : null;
  const visibleNotes = [...notes]
    .filter((note) => noteLinksVisibleWork(note, taskIds, visibleObjectiveIds))
    .sort((left, right) => left.id.localeCompare(right.id));
  const visibleNoteIds = new Set(visibleNotes.map(({ id }) => id));
  return {
    sortedTasks,
    visibleObjectives,
    visibleNotes,
    displayedKeyResults,
    activeTaskSelection,
    activeObjectiveSelection,
    activeNoteSelection:
      selectedNoteId && visibleNoteIds.has(selectedNoteId) ? selectedNoteId : null,
    graphKeyResultNode: matchingGraphKeyResult(displayedKeyResults, selectedGraphKeyResult),
    taskIds,
    visibleObjectiveIds,
    visibleNoteIds,
  };
}

function selectionFlowIdFor(visible: VisibleGraphEntities) {
  if (visible.graphKeyResultNode) return flowNodeId("key-result", visible.graphKeyResultNode.key);
  if (visible.activeNoteSelection) return flowNodeId("note", visible.activeNoteSelection);
  if (visible.activeTaskSelection) return flowNodeId("task", visible.activeTaskSelection);
  if (visible.activeObjectiveSelection) return flowNodeId("objective", visible.activeObjectiveSelection);
  return null;
}

function mapPresentationEdges(
  graphEdges: ReturnType<typeof buildWorkspaceEdges>,
  live: LiveEntities,
) {
  return graphEdges
    .map((edge) => {
      const [sourceKinds, targetKinds] = endpointKindsFor(edge.kind);
      return {
        ...edge,
        source: presentationEndpoint(edge.source, sourceKinds, live),
        target: presentationEndpoint(edge.target, targetKinds, live),
      };
    })
    .filter((edge): edge is typeof edge & { source: string; target: string } =>
      edge.source !== null && edge.target !== null);
}

function keyResultIsSelected(
  node: KeyResultNode,
  outcome: OutcomeFilter | null,
  selectedGraphKeyResult: GraphKeyResultSelection | null,
) {
  return Boolean(
    (outcome
      && outcome.kind === "pair"
      && outcome.objectiveId === node.objectiveId
      && outcome.keyResultId === node.keyResultId)
    || (selectedGraphKeyResult
      && selectedGraphKeyResult.objectiveId === node.objectiveId
      && selectedGraphKeyResult.keyResultId === node.keyResultId),
  );
}

function keyResultFlowNode(
  node: KeyResultNode,
  index: number,
  count: number,
  related: boolean,
  outcome: OutcomeFilter | null,
  selectedGraphKeyResult: GraphKeyResultSelection | null,
): WorkspaceFlowNode {
  return {
    id: flowNodeId("key-result", node.key),
    type: "workspace",
    position: nodePosition("key-result", index, count),
    ...INITIAL_NODE_DIMENSIONS["key-result"],
    draggable: true,
    dragHandle: GRAPH_MOVE_HANDLE_SELECTOR,
    data: {
      id: node.keyResultId,
      kind: "key-result",
      title: node.text,
      eyebrow: `${node.objectiveId} · ${node.keyResultId}`,
      contextCount: 0,
      selected: keyResultIsSelected(node, outcome, selectedGraphKeyResult),
      related,
      outcome: {
        objectiveId: node.objectiveId,
        keyResultId: node.keyResultId,
        recordedProgress: node.recordedProgress,
        target: node.target,
        status: node.status,
        linkedTotal: node.counts.total,
        visibleTotal: node.visibleCounts.total,
      },
    },
  };
}

function objectiveFlowNode(
  objective: WorkspaceObjective,
  index: number,
  count: number,
  selected: boolean,
  related: boolean,
): WorkspaceFlowNode {
  return {
    id: flowNodeId("objective", objective.id),
    type: "workspace",
    position: nodePosition("objective", index, count),
    ...INITIAL_NODE_DIMENSIONS.objective,
    draggable: true,
    dragHandle: GRAPH_MOVE_HANDLE_SELECTOR,
    selectable: false,
    data: {
      id: objective.id,
      kind: "objective",
      title: objectiveTitle(objective),
      eyebrow: `${objective.id} · ${objective.quarter || "Objective"}`,
      contextCount: 0,
      selected,
      related,
    },
  };
}

function taskFlowNode(
  task: WorkspaceTask,
  index: number,
  count: number,
  selected: boolean,
  related: boolean,
): WorkspaceFlowNode {
  return {
    id: flowNodeId("task", task.id),
    type: "workspace",
    position: nodePosition("task", index, count),
    ...INITIAL_NODE_DIMENSIONS.task,
    draggable: true,
    dragHandle: GRAPH_MOVE_HANDLE_SELECTOR,
    data: {
      id: task.id,
      kind: "task",
      title: task.title,
      eyebrow: task.id,
      contextCount: Math.max(0, task.context_count || 0),
      priority: asTaskPriority(task.priority),
      status: asTaskStatus(task.status),
      selected,
      related,
    },
  };
}

function noteFlowNode(
  note: WorkspaceNote,
  index: number,
  count: number,
  selected: boolean,
  related: boolean,
): WorkspaceFlowNode {
  return {
    id: flowNodeId("note", note.id),
    type: "workspace",
    position: nodePosition("note", index, count),
    ...INITIAL_NODE_DIMENSIONS.note,
    draggable: true,
    dragHandle: GRAPH_MOVE_HANDLE_SELECTOR,
    selectable: false,
    data: {
      id: note.id,
      kind: "note",
      title: noteTitle(note),
      eyebrow: `${note.id} · ${note.created || "Context card"}`,
      contextCount: 0,
      selected,
      related,
    },
  };
}

function planningFlowEdge(
  edge: { id: string; source: string; target: string; kind: CanonicalEdgeKind },
  selectionFlowId: string | null,
): WorkspaceFlowEdge {
  const hot =
    !!selectionFlowId &&
    (edge.source === selectionFlowId || edge.target === selectionFlowId);
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: "planning",
    animated: edge.kind === "dependency" && hot,
    markerEnd: {
      type: MarkerType.ArrowClosed,
      color: EDGE_COLORS[edge.kind],
      width: 14,
      height: 14,
    },
    style: {
      stroke: EDGE_COLORS[edge.kind],
      strokeWidth: hot ? 2.8 : 1.4,
      strokeDasharray:
        edge.kind === "dependency"
          ? "8 5"
          : edge.kind === "reference"
            ? "3 5"
            : undefined,
      opacity: selectionFlowId && !hot ? 0.12 : 0.74,
    },
    data: { kind: edge.kind },
  };
}

function assembleGraphNodes(
  visible: VisibleGraphEntities,
  isRelated: (id: string) => boolean,
  outcome: OutcomeFilter | null,
  selectedGraphKeyResult: GraphKeyResultSelection | null,
): WorkspaceFlowNode[] {
  const { displayedKeyResults, visibleObjectives, sortedTasks, visibleNotes } = visible;
  return [
    ...displayedKeyResults.map((node, index) => keyResultFlowNode(
      node,
      index,
      displayedKeyResults.length,
      isRelated(flowNodeId("key-result", node.key)),
      outcome,
      selectedGraphKeyResult,
    )),
    ...visibleObjectives.map((objective, index) => objectiveFlowNode(
      objective,
      index,
      visibleObjectives.length,
      objective.id === visible.activeObjectiveSelection,
      isRelated(flowNodeId("objective", objective.id)),
    )),
    ...sortedTasks.map((task, index) => taskFlowNode(
      task,
      index,
      sortedTasks.length,
      task.id === visible.activeTaskSelection,
      isRelated(flowNodeId("task", task.id)),
    )),
    ...visibleNotes.map((note, index) => noteFlowNode(
      note,
      index,
      visibleNotes.length,
      note.id === visible.activeNoteSelection,
      isRelated(flowNodeId("note", note.id)),
    )),
  ];
}

export function shouldVirtualizeGraph(nodeCount: number) {
  return nodeCount > GRAPH_VIRTUALIZATION_THRESHOLD;
}

export function makeGraphModel(
  tasks: readonly WorkspaceTask[],
  objectives: readonly WorkspaceObjective[],
  notes: readonly WorkspaceNote[],
  edges: readonly WorkspaceEdge[],
  selectedTaskId: string | null | undefined,
  selectedObjectiveId: string | null | undefined = null,
  keyResultProjection: KeyResultProjection | null = null,
  outcome: OutcomeFilter | null = null,
  selectedNoteId: string | null | undefined = null,
  selectedGraphKeyResult: GraphKeyResultSelection | null = null,
) {
  const visible = visibleGraphEntities(
    tasks,
    objectives,
    notes,
    selectedTaskId,
    selectedObjectiveId,
    keyResultProjection,
    selectedNoteId,
    selectedGraphKeyResult,
  );
  const knownIds = new Set([
    ...visible.taskIds,
    ...visible.visibleObjectiveIds,
    ...visible.visibleNoteIds,
  ]);
  const graphEdges = buildWorkspaceEdges(visible.sortedTasks, visible.visibleNotes, edges).filter(
    (edge) => knownIds.has(edge.source) && knownIds.has(edge.target),
  );
  const selectionFlowId = selectionFlowIdFor(visible);
  const liveEntities = {
    tasks: visible.taskIds,
    objectives: visible.visibleObjectiveIds,
    notes: visible.visibleNoteIds,
  };
  const presentationEdges = mapPresentationEdges(graphEdges, liveEntities);
  const outcomeEdges = keyResultProjection
    ? deriveOutcomeEdges(keyResultProjection, visible.sortedTasks)
    : [];
  const outcomeFlowPairs = outcomeFlowPairsFor(
    outcomeEdges,
    visible.visibleObjectives,
    visible.sortedTasks,
    visible.displayedKeyResults,
  );
  const relatedIds = relatedPresentationIds(selectionFlowId, [
    ...presentationEdges,
    ...outcomeFlowPairs,
  ]);
  const isRelated = (id: string) => !selectionFlowId || relatedIds.has(id);
  const nodes = assembleGraphNodes(visible, isRelated, outcome, selectedGraphKeyResult);
  const flowEdges = presentationEdges.map((edge) => planningFlowEdge(edge, selectionFlowId));
  const derivedEdges = buildOutcomeFlowEdges(
    outcomeEdges,
    nodes,
    visible.visibleObjectives,
    visible.sortedTasks,
    visible.displayedKeyResults,
  );
  return { nodes, edges: [...flowEdges, ...derivedEdges] };
}
