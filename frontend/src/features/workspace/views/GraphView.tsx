import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Background,
  BaseEdge,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  getSmoothStepPath,
  type Edge as FlowEdge,
  type EdgeProps,
  type MiniMapNodeProps,
  type Node as FlowNode,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { GraphContextPopover } from "./GraphContextPopover";
import "./GraphContextPopover.css";

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
import {
  DERIVED_KEY_RESULT_OBJECTIVE,
  DERIVED_TASK_KEY_RESULT,
  deriveOutcomeEdges,
  keyResultCatalog,
  keyResultEndpointKey,
  objectiveEndpointKey,
  taskEndpointKey,
  type DerivedEdgeKind,
  type DerivedOutcomeEdge,
} from "./keyResultViewModel";
import type { KeyResultNode, KeyResultProjection, OutcomeFilter } from "./keyResultModel";
import {
  GRAPH_NODE_SIZES,
  layoutPlanningGraph,
  type GraphEdgeRoute,
  type GraphRoutePoint,
} from "./graphLayout";

type GraphNodeKind = "objective" | "task" | "note" | "key-result";

export type GraphNodeData = Record<string, unknown> & {
  id: string;
  kind: GraphNodeKind;
  title: string;
  eyebrow: string;
  contextCount: number;
  priority?: string;
  status?: string;
  selected: boolean;
  related: boolean;
  /** Key-result presentation only; never canonical Task or Objective data. */
  outcome?: {
    objectiveId: string;
    keyResultId: string;
    /** Recorded value, or null when the workspace never recorded one. */
    recordedProgress: number | null;
    target: string | null;
    status: string | null;
    linkedTotal: number;
    visibleTotal: number;
  };
  onActivate?: () => void;
  onOpenContext?: (trigger: HTMLButtonElement) => void;
};

type WorkspaceFlowNode = FlowNode<GraphNodeData, "workspace">;

interface GraphViewProps {
  /** Already-projected, visible Tasks. Never filtered again here. */
  tasks: readonly WorkspaceTask[];
  /**
   * Canonical Tasks. Popup identity resolves against these, so completion or a
   * filter cannot dismiss an open popup. Defaults to tasks for isolated callers;
   * the real Workspace path always supplies the canonical array.
   */
  referenceTasks?: readonly WorkspaceTask[];
  objectives: readonly WorkspaceObjective[];
  notes: readonly WorkspaceNote[];
  edges: readonly WorkspaceEdge[];
  emptyKind?: string;
  selectedTaskId?: string | null;
  selectedObjectiveId?: string | null;
  contextTargetTaskId?: string | null;
  focusPinnedTaskId?: string | null;
  onContextTargetChange?: (taskId: string | null) => void;
  onFocusPinChange?: (taskId: string | null) => void;
  /** Page-owned prerequisite controls rendered inside the context modal. */
  renderPopupPrerequisites?: (taskId: string) => ReactNode;
  /** Supplied outcome projection; this view never builds its own. */
  keyResultProjection?: KeyResultProjection | null;
  /** The already-normalized scoped coordinate; selection display only. */
  outcome?: OutcomeFilter | null;
  /** The exact scoped outcome-filter callback. No other effect. */
  onSelectOutcome?: (selection: { objectiveId: string; keyResultId: string }) => void;
  onSelectTask: (taskId: string) => void;
  onSelectObjective: (objectiveId: string) => void;
}

const EDGE_COLORS = {
  alignment: "var(--wsv-relation-alignment)",
  dependency: "var(--wsv-relation-dependency)",
  parent: "var(--wsv-relation-parent)",
  reference: "var(--wsv-relation-reference)",
} as const;

type GraphEdgeData = Record<string, unknown> & {
  /** Canonical relationship kinds plus the separate derived presentation kinds. */
  kind: keyof typeof EDGE_COLORS | DerivedEdgeKind;
  route?: GraphEdgeRoute;
};

type WorkspaceFlowEdge = FlowEdge<GraphEdgeData>;

function offsetToward(from: GraphRoutePoint, to: GraphRoutePoint, distance: number) {
  const length = Math.abs(to.x - from.x) + Math.abs(to.y - from.y);
  if (!length) return from;
  const ratio = Math.min(1, distance / length);
  return {
    x: from.x + (to.x - from.x) * ratio,
    y: from.y + (to.y - from.y) * ratio,
  };
}

export function roundedOrthogonalPath(points: readonly GraphRoutePoint[], radius = 10) {
  if (!points.length) return "";
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
  let path = `M ${points[0].x} ${points[0].y}`;
  for (let index = 1; index < points.length - 1; index += 1) {
    const previous = points[index - 1];
    const corner = points[index];
    const next = points[index + 1];
    const incoming = Math.abs(corner.x - previous.x) + Math.abs(corner.y - previous.y);
    const outgoing = Math.abs(next.x - corner.x) + Math.abs(next.y - corner.y);
    const bendRadius = Math.min(radius, incoming / 2, outgoing / 2);
    const before = offsetToward(corner, previous, bendRadius);
    const after = offsetToward(corner, next, bendRadius);
    path += ` L ${before.x} ${before.y} Q ${corner.x} ${corner.y} ${after.x} ${after.y}`;
  }
  const last = points[points.length - 1];
  return `${path} L ${last.x} ${last.y}`;
}

function PlanningEdge({
  data,
  id,
  markerEnd,
  sourcePosition,
  sourceX,
  sourceY,
  style,
  targetPosition,
  targetX,
  targetY,
}: EdgeProps<WorkspaceFlowEdge>) {
  const [fallbackPath, fallbackLabelX, fallbackLabelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
    borderRadius: 10,
  });
  const path = data?.route ? roundedOrthogonalPath(data.route.points) : fallbackPath;
  const label = data?.route?.label ?? { x: fallbackLabelX, y: fallbackLabelY };
  return (
    <BaseEdge
      id={id}
      path={path}
      markerEnd={markerEnd}
      style={style}
      label={data?.kind}
      labelX={label.x}
      labelY={label.y}
      labelStyle={{ fill: "var(--wsv-graph-edge-label-text)", fontSize: 9, fontWeight: 700 }}
      labelBgStyle={{ fill: "var(--wsv-graph-edge-label-bg)", fillOpacity: 0.84 }}
      labelBgBorderRadius={4}
      labelBgPadding={[4, 2]}
    />
  );
}

const OUTCOME_EDGE_COLORS = {
  [DERIVED_TASK_KEY_RESULT]: "var(--wsv-relation-outcome-task)",
  [DERIVED_KEY_RESULT_OBJECTIVE]: "var(--wsv-relation-outcome-objective)",
} as const;

const EDGE_TYPES = { planning: PlanningEdge };

export function GraphNodeFrame({
  after,
  before,
  data,
}: {
  after?: ReactNode;
  before?: ReactNode;
  data: GraphNodeData;
}) {
  const actionable = Boolean(data.onActivate);
  return (
    <div
      className={[
        "wsv-graph-node",
        `wsv-graph-node--${data.kind}`,
        data.selected ? "is-selected" : "",
        !data.related ? "is-muted" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {before}
      <div
        className="wsv-graph-node__action"
        aria-label={actionable
          ? data.outcome
            // Naming says what activation does: it selects a filter.
            ? `Filter by key result ${data.outcome.objectiveId} ${data.outcome.keyResultId}`
            : `${data.kind === "objective" ? "Focus" : "Open"} ${data.kind} ${data.id}`
          : undefined}
        onClick={actionable ? (event) => {
          event.stopPropagation();
          data.onActivate?.();
        } : undefined}
        onKeyDown={actionable ? (event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          event.stopPropagation();
          data.onActivate?.();
        } : undefined}
        role={actionable ? "button" : undefined}
        tabIndex={actionable ? 0 : undefined}
      >
        <div className="wsv-graph-node__eyebrow">{data.eyebrow}</div>
        <div className="wsv-graph-node__title" title={data.title}>
          {data.title}
        </div>
        {data.kind === "task" ? (
          <div className="wsv-graph-node__meta">
            <span className={`wsv-priority wsv-priority--${data.priority}`}>
              {data.priority}
            </span>
            <span>{data.status}</span>
          </div>
        ) : null}
      </div>
      {data.kind === "task" && data.contextCount > 0 ? (
        <button
          type="button"
          className="wsv-context-badge wsv-graph-context-trigger nodrag nopan"
          aria-label={`Open context for task ${data.id}: ${data.contextCount} linked context items`}
          aria-haspopup="dialog"
          onPointerDown={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
          onClick={(event) => {
            event.stopPropagation();
            data.onOpenContext?.(event.currentTarget);
          }}
        >
          ↗ {data.contextCount}
        </button>
      ) : null}
      {data.outcome ? (
        <dl className="wsv-graph-node__outcome">
          <div>
            <dt>Progress</dt>
            <dd>
              {data.outcome.recordedProgress === null
                ? "Unrecorded"
                : String(data.outcome.recordedProgress)}
            </dd>
          </div>
          {data.outcome.target !== null ? (
            <div>
              <dt>Target</dt>
              <dd>{data.outcome.target}</dd>
            </div>
          ) : null}
          {data.outcome.status !== null ? (
            <div>
              <dt>Status</dt>
              <dd>{data.outcome.status}</dd>
            </div>
          ) : null}
          <div>
            <dt>Tasks</dt>
            <dd>
              {`${data.outcome.linkedTotal} linked`}
              {` · ${data.outcome.visibleTotal} of ${data.outcome.linkedTotal} visible`}
            </dd>
          </div>
        </dl>
      ) : null}
      {after}
    </div>
  );
}

function WorkspaceNode({ data }: NodeProps<WorkspaceFlowNode>) {
  return (
    <GraphNodeFrame
      before={<Handle type="target" position={Position.Left} className="wsv-handle" />}
      data={data}
      after={<Handle type="source" position={Position.Right} className="wsv-handle" />}
    />
  );
}

const NODE_TYPES = { workspace: WorkspaceNode };
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

export function miniMapNodeColor(node: WorkspaceFlowNode) {
  if (node.data.kind === "objective") return "var(--wsv-minimap-node-objective)";
  if (node.data.kind === "note") return "var(--wsv-minimap-node-note)";
  if (node.data.kind === "key-result") return "var(--wsv-minimap-node-key-result)";
  return "var(--wsv-minimap-node-task)";
}

export function miniMapNodeClassName(node: WorkspaceFlowNode) {
  return [
    "wsv-minimap-node",
    `wsv-minimap-node--${node.data.kind}`,
    node.data.selected ? "is-selected" : "",
    !node.data.related ? "is-muted" : "",
  ].filter(Boolean).join(" ");
}

function WorkspaceMiniMapNode({
  borderRadius,
  className,
  color,
  height,
  shapeRendering,
  strokeColor,
  strokeWidth,
  width,
  x,
  y,
}: MiniMapNodeProps) {
  return (
    <rect
      aria-hidden="true"
      className={`react-flow__minimap-node ${className}`}
      fill={color}
      height={height}
      rx={Math.min(borderRadius, width / 2, height / 2)}
      ry={Math.min(borderRadius, width / 2, height / 2)}
      shapeRendering={shapeRendering}
      stroke={strokeColor}
      strokeWidth={strokeWidth}
      vectorEffect="non-scaling-stroke"
      width={width}
      x={x}
      y={y}
    />
  );
}

export function shouldVirtualizeGraph(nodeCount: number) {
  return nodeCount > GRAPH_VIRTUALIZATION_THRESHOLD;
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
 * Derived presentation edges. They never go through canonicalEdgeKind and
 * workspace.edges is not mutated. An endpoint with no live node is dropped
 * rather than fabricating a resolved one.
 */
/**
 * GR02: live Flow identity is a typed presentation value, not a canonical ID.
 * A schema-valid Objective ID may literally equal an escaped key-result
 * endpoint string, so every kind is namespaced and every segment escaped.
 * Canonical IDs, relationships and callback values are untouched.
 */
function flowNodeId(kind: "task" | "objective" | "note" | "key-result", raw: string) {
  const escaped = raw.replace(/\\/g, "\\\\").replace(/\|/g, "\\|");
  return ["flow", kind, escaped].join("|");
}

/**
 * GN1: a canonical endpoint is resolved to its live presentation node by the
 * entity kind the relationship itself implies, never by raw string identity.
 * Canonical relationships, their count and their kinds are untouched.
 */
type GraphEntityKind = "task" | "objective" | "note";

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

/**
 * Resolve one endpoint to a live presentation node of an ALLOWED kind. A
 * required kind that is not on the canvas yields null: the presentation edge
 * is then omitted rather than redirected onto an unrelated same-ID node. No
 * node is manufactured and no canonical relationship is rewritten.
 */
function presentationEndpoint(
  rawId: string,
  allowed: readonly GraphEntityKind[],
  live: { tasks: ReadonlySet<string>; objectives: ReadonlySet<string>; notes: ReadonlySet<string> },
) {
  for (const kind of allowed) {
    const known = kind === "task" ? live.tasks : kind === "objective" ? live.objectives : live.notes;
    if (known.has(rawId)) return flowNodeId(kind, rawId);
  }
  return null;
}

/** GN2: relatedness travels on typed identities, so no raw ID leaks across kinds. */
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

function buildOutcomeFlowEdges(
  outcomeEdges: readonly DerivedOutcomeEdge[],
  nodes: readonly WorkspaceFlowNode[],
  visibleObjectives: readonly WorkspaceObjective[],
  sortedTasks: readonly WorkspaceTask[],
  keyResults: readonly KeyResultNode[],
): WorkspaceFlowEdge[] {
  const liveNodeIds = new Set(nodes.map((node) => node.id));
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

export function makeGraphModel(
  tasks: readonly WorkspaceTask[],
  objectives: readonly WorkspaceObjective[],
  notes: readonly WorkspaceNote[],
  edges: readonly WorkspaceEdge[],
  selectedTaskId: string | null | undefined,
  selectedObjectiveId: string | null | undefined = null,
  keyResultProjection: KeyResultProjection | null = null,
  outcome: OutcomeFilter | null = null,
) {
  const sortedTasks = [...tasks].sort((left, right) => {
    const objectiveDelta = (left.objective_ids?.[0] || "ZZZ").localeCompare(
      right.objective_ids?.[0] || "ZZZ",
    );
    return objectiveDelta || left.id.localeCompare(right.id);
  });
  const taskIds = new Set(sortedTasks.map((task) => task.id));
  const activeTaskSelection =
    selectedTaskId && taskIds.has(selectedTaskId) ? selectedTaskId : null;
  const displayedKeyResults: KeyResultNode[] = keyResultProjection
    ? keyResultCatalog(keyResultProjection)
    : [];
  const objectiveIds = new Set([
    ...sortedTasks.flatMap((task) => [...(task.objective_ids || [])]),
    // GR01: a displayed Key Result keeps its real parent Objective node, so its
    // derived KR -> Objective edge always has a live endpoint. Only uniquely
    // resolvable parents reach this set; nothing is fabricated.
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
  const activeSelection = activeTaskSelection ?? activeObjectiveSelection;
  const visibleNotes = [...notes]
    .filter((note) =>
      (note.links || []).some(
        (id) => taskIds.has(id) || visibleObjectiveIds.has(id),
      ),
    )
    .sort((left, right) => left.id.localeCompare(right.id));
  const visibleNoteIds = new Set(visibleNotes.map(({ id }) => id));
  const knownIds = new Set([
    ...taskIds,
    ...visibleObjectiveIds,
    ...visibleNoteIds,
  ]);
  const graphEdges = buildWorkspaceEdges(sortedTasks, visibleNotes, edges).filter(
    (edge) => knownIds.has(edge.source) && knownIds.has(edge.target),
  );

  const selectionFlowId = activeTaskSelection
    ? flowNodeId("task", activeTaskSelection)
    : activeObjectiveSelection
      ? flowNodeId("objective", activeObjectiveSelection)
      : null;
  const liveEntities = { tasks: taskIds, objectives: visibleObjectiveIds, notes: visibleNoteIds };
  // GN1: the ordinary relationship layer now maps endpoints exactly the way the
  // derived layer already does. Nothing is dropped to make endpoints match.
  const presentationEdges = graphEdges
    .map((edge) => {
      const [sourceKinds, targetKinds] = endpointKindsFor(edge.kind);
      return {
        ...edge,
        source: presentationEndpoint(edge.source, sourceKinds, liveEntities),
        target: presentationEndpoint(edge.target, targetKinds, liveEntities),
      };
    })
    // GC-F1: an edge whose required typed endpoint is absent is not drawn. The
    // canonical arrays, counts and readiness data above are untouched.
    .filter((edge): edge is typeof edge & { source: string; target: string } =>
      edge.source !== null && edge.target !== null);
  const relatedIds = relatedPresentationIds(selectionFlowId, presentationEdges);
  const isRelated = (id: string) => !activeSelection || relatedIds.has(id);

  // Presentation-only outcome layer. It reuses the admitted pure helpers
  // and never re-derives filter, readiness, status or progress data.
  const keyResults: KeyResultNode[] = displayedKeyResults;
  const outcomeEdges = keyResultProjection
    ? deriveOutcomeEdges(keyResultProjection, sortedTasks)
    : [];

  const nodes: WorkspaceFlowNode[] = [
    ...keyResults.map((node, index) => ({
      // Collision-safe scoped identity: the same local KR under a different
      // Objective, and delimiter-bearing IDs, stay distinct nodes.
      id: flowNodeId("key-result", node.key),
      type: "workspace" as const,
      position: nodePosition("key-result", index, keyResults.length),
      ...INITIAL_NODE_DIMENSIONS["key-result"],
      draggable: false,
      data: {
        id: node.keyResultId,
        kind: "key-result" as const,
        title: node.text,
        eyebrow: `${node.objectiveId} · ${node.keyResultId}`,
        contextCount: 0,
        // GR05: derived from the existing coordinate; only an exact scoped pair
        // selects, and the same local ID under another parent stays unselected.
        selected: Boolean(
          outcome
            && outcome.kind === "pair"
            && outcome.objectiveId === node.objectiveId
            && outcome.keyResultId === node.keyResultId,
        ),
        related: isRelated(flowNodeId("key-result", node.key)),
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
    })),
    ...visibleObjectives.map((objective, index) => ({
      id: flowNodeId("objective", objective.id),
      type: "workspace" as const,
      position: nodePosition("objective", index, visibleObjectives.length),
      ...INITIAL_NODE_DIMENSIONS.objective,
      draggable: false,
      selectable: false,
      data: {
        id: objective.id,
        kind: "objective" as const,
        title: objectiveTitle(objective),
        eyebrow: `${objective.id} · ${objective.quarter || "Objective"}`,
        contextCount: 0,
        selected: objective.id === activeObjectiveSelection,
        related: isRelated(flowNodeId("objective", objective.id)),
      },
    })),
    ...sortedTasks.map((task, index) => ({
      id: flowNodeId("task", task.id),
      type: "workspace" as const,
      position: nodePosition("task", index, sortedTasks.length),
      ...INITIAL_NODE_DIMENSIONS.task,
      draggable: false,
      data: {
        id: task.id,
        kind: "task" as const,
        title: task.title,
        eyebrow: task.id,
        contextCount: Math.max(0, task.context_count || 0),
        priority: asTaskPriority(task.priority),
        status: asTaskStatus(task.status),
        selected: task.id === activeTaskSelection,
        related: isRelated(flowNodeId("task", task.id)),
      },
    })),
    ...visibleNotes.map((note, index) => ({
      id: flowNodeId("note", note.id),
      type: "workspace" as const,
      position: nodePosition("note", index, visibleNotes.length),
      ...INITIAL_NODE_DIMENSIONS.note,
      draggable: false,
      selectable: false,
      data: {
        id: note.id,
        kind: "note" as const,
        title: noteTitle(note),
        eyebrow: `${note.id} · ${note.created || "Context card"}`,
        contextCount: 0,
        selected: false,
        related: isRelated(flowNodeId("note", note.id)),
      },
    })),
  ];

  const flowEdges: WorkspaceFlowEdge[] = presentationEdges.map((edge) => {
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
        opacity: activeSelection && !hot ? 0.12 : 0.74,
      },
      data: { kind: edge.kind },
    };
  });

  const derivedEdges = buildOutcomeFlowEdges(
    outcomeEdges,
    nodes,
    visibleObjectives,
    sortedTasks,
    keyResults,
  );

  return { nodes, edges: [...flowEdges, ...derivedEdges] };
}

export function GraphView({
  tasks,
  objectives,
  referenceTasks,
  notes,
  edges,
  emptyKind = "none",
  selectedTaskId,
  selectedObjectiveId,
  contextTargetTaskId = null,
  focusPinnedTaskId = null,
  onContextTargetChange,
  onFocusPinChange,
  renderPopupPrerequisites,
  keyResultProjection = null,
  outcome = null,
  onSelectOutcome,
  onSelectTask,
  onSelectObjective,
}: GraphViewProps) {
  const canonicalTasks = referenceTasks ?? tasks;
  // Each popup open is a distinct interaction lifetime. A release callback may
  // only end the lifetime that created it, so an older popup's blur can never
  // clear a pin a newer popup just acquired for the same trigger.
  const pinGenerationRef = useRef(0);
  // Liveness for deferred restoration work owned by this Graph.
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);
  // The DOM trigger stays local to Graph; only the Task id crosses the boundary.
  const [contextTrigger, setContextTrigger] = useState<HTMLButtonElement | null>(null);
  const [localContextTaskId, setLocalContextTaskId] = useState<string | null>(null);
  const contextTargetId = onContextTargetChange ? contextTargetTaskId : localContextTaskId;
  const setContextTargetId = useCallback((taskId: string | null) => {
    setLocalContextTaskId(taskId);
    onContextTargetChange?.(taskId);
  }, [onContextTargetChange]);

  // Popup identity is canonical: hiding or completing a Task must not close it.
  const contextTask = canonicalTasks.find((task) => task.id === contextTargetId);
  useEffect(() => {
    // Canonical deletion, not visibility filtering, dismisses the popup.
    if (contextTargetId && !contextTask) {
      setLocalContextTaskId(null);
      onContextTargetChange?.(null);
      setContextTrigger(null);
    }
  }, [contextTargetId, contextTask, onContextTargetChange]);
  const model = useMemo(
    () => makeGraphModel(
      tasks,
      objectives,
      notes,
      edges,
      selectedTaskId,
      selectedObjectiveId,
      keyResultProjection,
      outcome,
    ),
    [
      tasks,
      objectives,
      notes,
      edges,
      selectedTaskId,
      selectedObjectiveId,
      keyResultProjection,
      outcome,
    ],
  );
  // Layout is keyed by the model it was computed for. Rendered membership and
  // task data always come from the CURRENT model; a layout result contributes
  // positions and routes only while it is still compatible, so a stale result
  // can never restore a hidden node or route an obsolete edge.
  const [layout, setLayout] = useState<{
    model: typeof model | null;
    nodes: WorkspaceFlowNode[];
    edgeRoutes: Record<string, GraphEdgeRoute>;
  }>({ model: null, nodes: [], edgeRoutes: {} });
  useEffect(() => {
    let cancelled = false;
    void layoutPlanningGraph(model.nodes, model.edges).then((result) => {
      if (!cancelled) setLayout({ model, nodes: result.nodes, edgeRoutes: result.edgeRoutes });
    }).catch(() => {
      if (!cancelled) setLayout({ model, nodes: model.nodes, edgeRoutes: {} });
    });
    return () => { cancelled = true; };
  }, [model]);

  const compatibleLayout = layout.model === model ? layout : null;
  const positionsById = useMemo(() => new Map(
    (compatibleLayout?.nodes ?? []).map((node) => [node.id, node]),
  ), [compatibleLayout]);

  // Synchronous: membership and data are the current model's, in its order.
  const positionedNodes = useMemo(
    () => model.nodes.map((node) => {
      const laid = positionsById.get(node.id);
      return laid ? { ...node, position: laid.position } : node;
    }),
    [model.nodes, positionsById],
  );

  const interactiveNodes = useMemo(
    () => positionedNodes.map((node) => ({
      ...node,
      data: {
        ...node.data,
        onActivate: node.data.kind === "task"
          ? () => onSelectTask(node.data.id)
          : node.data.kind === "objective"
            ? () => onSelectObjective(node.data.id)
            : node.data.kind === "key-result" && node.data.outcome && onSelectOutcome
              // Filter selection only: never Task or Objective selection,
              // the context dialog, a write or a camera action.
              ? () => onSelectOutcome({
                  objectiveId: node.data.outcome!.objectiveId,
                  keyResultId: node.data.outcome!.keyResultId,
                })
              : undefined,
        onOpenContext: node.data.kind === "task"
          ? (trigger: HTMLButtonElement) => {
              setContextTrigger(trigger);
              setContextTargetId(node.data.id);
              // The focus pin is independent of both shell selection and popup.
              pinGenerationRef.current += 1;
              onFocusPinChange?.(node.data.id);
            }
          : undefined,
      },
    })),
    [
      positionedNodes,
      onSelectObjective,
      onSelectTask,
      onSelectOutcome,
      setContextTargetId,
      onFocusPinChange,
    ],
  );
  const routedEdges = useMemo(() => model.edges.map((edge) => ({
    ...edge,
    data: {
      ...edge.data,
      // Routes only from a layout computed for this exact model.
      route: compatibleLayout?.edgeRoutes[edge.id],
    },
  })), [compatibleLayout, model.edges]);

  // Bound to the generation that was current when the popup opened.
  const releaseFocusPinFor = (generation: number) => () => {
    if (pinGenerationRef.current !== generation) return;
    onFocusPinChange?.(null);
  };

  // GR01: emptiness is about rendered content. A zero-Task workspace with a
  // real Key Result still has a usable graph, so no overlay covers it.
  const empty = !model.nodes.length;

  return (
    <div className="wsv-graph" aria-label="Task relationship graph" data-graph-empty={empty ? "true" : "false"}>
      {contextTargetId && contextTask ? (
        <GraphContextPopover
          key={contextTargetId}
          taskId={contextTargetId}
          taskTitle={contextTask.title}
          trigger={contextTrigger}
          focusFallbackSelector="[data-workspace-focus-fallback]"
          ownerAliveRef={aliveRef}
          prerequisites={renderPopupPrerequisites?.(contextTargetId)}
          onClose={() => {
            setContextTargetId(null);
            setContextTrigger(null);
          }}
          onFocusReturned={releaseFocusPinFor(pinGenerationRef.current)}
          onOpenTask={() => {
            const openedId = contextTargetId;
            setContextTargetId(null);
            setContextTrigger(null);
            onFocusPinChange?.(null);
            onSelectTask(openedId);
          }}
        />
      ) : null}
      {empty ? (
        // Overlay, not an early return: the mounted canvas and its viewport
        // survive a nonempty -> empty -> nonempty projection change.
        <div className="wsv-graph-empty-overlay" role="status">
          <strong>{graphEmptyTitle(emptyKind)}</strong>
          <span>{graphEmptyDetail(emptyKind)}</span>
        </div>
      ) : null}
      <ReactFlow
        nodes={interactiveNodes}
        edges={routedEdges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onNodeClick={(_, node) => {
          if (node.data.kind === "task") onSelectTask(node.data.id);
          if (node.data.kind === "objective") onSelectObjective(node.data.id);
        }}
        nodesDraggable={false}
        nodesFocusable={false}
        nodesConnectable={false}
        elementsSelectable
        onlyRenderVisibleElements={shouldVirtualizeGraph(model.nodes.length)}
        fitView
        fitViewOptions={{ padding: 0.12, minZoom: 0.42, maxZoom: 1.15 }}
        minZoom={0.25}
        maxZoom={1.7}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="var(--wsv-graph-grid)" gap={24} size={1} />
        <MiniMap
          ariaLabel="Workspace overview minimap"
          bgColor="var(--wsv-minimap-bg)"
          maskColor="var(--wsv-minimap-mask)"
          maskStrokeColor="var(--wsv-minimap-mask-border)"
          maskStrokeWidth={1.25}
          pannable
          zoomable
          nodeBorderRadius={8}
          nodeClassName={(node) => miniMapNodeClassName(node as WorkspaceFlowNode)}
          nodeColor={(node) => miniMapNodeColor(node as WorkspaceFlowNode)}
          nodeComponent={WorkspaceMiniMapNode}
          nodeStrokeColor={(node) => node.data?.selected
            ? "var(--wsv-minimap-node-selected)"
            : "var(--wsv-minimap-node-stroke)"}
          nodeStrokeWidth={3}
          offsetScale={10}
        />
        <Controls showInteractive={false} />
      </ReactFlow>
      <div className="wsv-graph-legend" aria-label="Relationship legend">
        {Object.entries(EDGE_COLORS).map(([label, color]) => (
          <span key={label}>
            <i style={{ background: color }} /> {label}
          </span>
        ))}
      </div>
    </div>
  );
}

function graphEmptyTitle(emptyKind: string) {
  if (emptyKind === "no-tasks") return "No tasks yet";
  if (emptyKind === "all-complete") return "All matching tasks are completed";
  return "No work matches these filters";
}

function graphEmptyDetail(emptyKind: string) {
  if (emptyKind === "no-tasks") return "Create a Task to start planning this workspace.";
  if (emptyKind === "all-complete") return "Show completed tasks to bring them back into view.";
  return "Clear a filter to bring tasks and their relationships back.";
}
