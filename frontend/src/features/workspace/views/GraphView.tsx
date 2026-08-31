import { useEffect, useMemo, useState, type ReactNode } from "react";
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
} from "./viewModels";
import {
  layoutPlanningGraph,
  type GraphEdgeRoute,
  type GraphRoutePoint,
} from "./graphLayout";

type GraphNodeKind = "objective" | "task" | "note";

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
  onActivate?: () => void;
};

type WorkspaceFlowNode = FlowNode<GraphNodeData, "workspace">;

interface GraphViewProps {
  tasks: readonly WorkspaceTask[];
  objectives: readonly WorkspaceObjective[];
  notes: readonly WorkspaceNote[];
  edges: readonly WorkspaceEdge[];
  selectedTaskId?: string | null;
  selectedObjectiveId?: string | null;
  onSelectTask: (taskId: string) => void;
  onSelectObjective: (objectiveId: string) => void;
}

const EDGE_COLORS = {
  alignment: "#8b7cf6",
  dependency: "#f06f7d",
  parent: "#44bfd1",
  reference: "#d7a94d",
} as const;

type GraphEdgeData = Record<string, unknown> & {
  kind: keyof typeof EDGE_COLORS;
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
      labelStyle={{ fill: "#90a1b8", fontSize: 9, fontWeight: 700 }}
      labelBgStyle={{ fill: "#0b1421", fillOpacity: 0.84 }}
      labelBgBorderRadius={4}
      labelBgPadding={[4, 2]}
    />
  );
}

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
      aria-label={actionable
        ? `${data.kind === "objective" ? "Focus" : "Open"} ${data.kind} ${data.id}`
        : undefined}
      className={[
        "wsv-graph-node",
        `wsv-graph-node--${data.kind}`,
        data.selected ? "is-selected" : "",
        !data.related ? "is-muted" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      onKeyDown={actionable ? (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        event.stopPropagation();
        data.onActivate?.();
      } : undefined}
      role={actionable ? "button" : undefined}
      tabIndex={actionable ? 0 : undefined}
    >
      {before}
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
          {data.contextCount > 0 ? (
            <span
              className="wsv-context-badge"
              aria-label={`${data.contextCount} linked context items`}
              title={`${data.contextCount} linked context items`}
            >
              ↗ {data.contextCount}
            </span>
          ) : null}
        </div>
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
} as const;

export function miniMapNodeColor(node: WorkspaceFlowNode) {
  if (node.data.kind === "objective") return "#8b7cf6";
  if (node.data.kind === "note") return "#d7a94d";
  return "#4ea8de";
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

  const columns = count > 16 ? 3 : 2;
  const column = index % columns;
  const row = Math.floor(index / columns);
  return { x: 350 + column * 268, y: 34 + row * 118 };
}

export function makeGraphModel(
  tasks: readonly WorkspaceTask[],
  objectives: readonly WorkspaceObjective[],
  notes: readonly WorkspaceNote[],
  edges: readonly WorkspaceEdge[],
  selectedTaskId: string | null | undefined,
  selectedObjectiveId: string | null | undefined = null,
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
  const objectiveIds = new Set(
    sortedTasks.flatMap((task) => [...(task.objective_ids || [])]),
  );
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

  const relatedIds = new Set<string>();
  if (activeSelection) {
    relatedIds.add(activeSelection);
    for (const edge of graphEdges) {
      if (edge.source === activeSelection) relatedIds.add(edge.target);
      if (edge.target === activeSelection) relatedIds.add(edge.source);
    }
  }
  const isRelated = (id: string) => !activeSelection || relatedIds.has(id);

  const nodes: WorkspaceFlowNode[] = [
    ...visibleObjectives.map((objective, index) => ({
      id: objective.id,
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
        related: isRelated(objective.id),
      },
    })),
    ...sortedTasks.map((task, index) => ({
      id: task.id,
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
        selected: task.id === activeSelection,
        related: isRelated(task.id),
      },
    })),
    ...visibleNotes.map((note, index) => ({
      id: note.id,
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
        related: isRelated(note.id),
      },
    })),
  ];

  const flowEdges: WorkspaceFlowEdge[] = graphEdges.map((edge) => {
    const hot =
      !!activeSelection &&
      (edge.source === activeSelection || edge.target === activeSelection);
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

  return { nodes, edges: flowEdges };
}

export function GraphView({
  tasks,
  objectives,
  notes,
  edges,
  selectedTaskId,
  selectedObjectiveId,
  onSelectTask,
  onSelectObjective,
}: GraphViewProps) {
  const model = useMemo(
    () => makeGraphModel(
      tasks,
      objectives,
      notes,
      edges,
      selectedTaskId,
      selectedObjectiveId,
    ),
    [tasks, objectives, notes, edges, selectedTaskId, selectedObjectiveId],
  );
  const [layout, setLayout] = useState({ nodes: model.nodes, edgeRoutes: {} as Record<string, GraphEdgeRoute> });
  useEffect(() => {
    let cancelled = false;
    setLayout({ nodes: model.nodes, edgeRoutes: {} });
    void layoutPlanningGraph(model.nodes, model.edges).then((result) => {
      if (!cancelled) setLayout(result);
    }).catch(() => {
      if (!cancelled) setLayout({ nodes: model.nodes, edgeRoutes: {} });
    });
    return () => { cancelled = true; };
  }, [model.edges, model.nodes]);
  const interactiveNodes = useMemo(
    () => layout.nodes.map((node) => ({
      ...node,
      data: {
        ...node.data,
        onActivate: node.data.kind === "task"
          ? () => onSelectTask(node.id)
          : node.data.kind === "objective"
            ? () => onSelectObjective(node.id)
            : undefined,
      },
    })),
    [layout.nodes, onSelectObjective, onSelectTask],
  );
  const routedEdges = useMemo(() => model.edges.map((edge) => ({
    ...edge,
    data: {
      ...edge.data,
      route: layout.edgeRoutes[edge.id],
    },
  })), [layout.edgeRoutes, model.edges]);

  if (!tasks.length) {
    return (
      <div className="wsv-empty" role="status">
        <strong>No work matches these filters</strong>
        <span>Clear a filter to bring tasks and their relationships back.</span>
      </div>
    );
  }

  return (
    <div className="wsv-graph" aria-label="Task relationship graph">
      <ReactFlow
        nodes={interactiveNodes}
        edges={routedEdges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onNodeClick={(_, node) => {
          if (node.data.kind === "task") onSelectTask(node.id);
          if (node.data.kind === "objective") onSelectObjective(node.id);
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
        <Background color="#31445d" gap={24} size={1} />
        <MiniMap
          ariaLabel="Workspace overview minimap"
          bgColor="#07111d"
          maskColor="rgba(4, 10, 18, 0.22)"
          maskStrokeColor="#8fbde1"
          maskStrokeWidth={1.25}
          pannable
          zoomable
          nodeBorderRadius={8}
          nodeClassName={(node) => miniMapNodeClassName(node as WorkspaceFlowNode)}
          nodeColor={(node) => miniMapNodeColor(node as WorkspaceFlowNode)}
          nodeComponent={WorkspaceMiniMapNode}
          nodeStrokeColor={(node) => node.data?.selected ? "#f5fbff" : "#0b1421"}
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
