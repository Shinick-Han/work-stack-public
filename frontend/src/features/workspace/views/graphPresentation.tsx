import type { KeyboardEvent, PointerEvent, ReactNode } from "react";
import {
  Background,
  BaseEdge,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  getSmoothStepPath,
  type EdgeProps,
  type MiniMapNodeProps,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import { GRAPH_DRAG_THRESHOLD_PX, GRAPH_MOVE_HANDLE_CLASS, GRAPH_MOVE_HANDLE_SELECTOR, GRAPH_ZOOM_MIN } from "./graphViewState";
import { roundedOrthogonalPath } from "./graphPath";
import { OUTCOME_LEGEND, shouldVirtualizeGraph } from "./graphViewModel";
import {
  EDGE_COLORS,
  type GraphNodeData,
  type WorkspaceFlowEdge,
  type WorkspaceFlowNode,
} from "./graphViewTypes";

function graphNodeActionLabel(data: GraphNodeData) {
  if (data.outcome) {
    return `Highlight key result ${data.outcome.objectiveId} ${data.outcome.keyResultId}`;
  }
  if (data.kind === "note") return `Show note ${data.id}`;
  return `${data.kind === "objective" ? "Focus" : "Open"} ${data.kind} ${data.id}`;
}

function graphNodeClassName(data: GraphNodeData) {
  return [
    "wsv-graph-node",
    `wsv-graph-node--${data.kind}`,
    data.selected ? "is-selected" : "",
    !data.related ? "is-muted" : "",
  ]
    .filter(Boolean)
    .join(" ");
}

function activateGraphNodeFromClick(
  event: { stopPropagation: () => void },
  onActivate?: () => void,
) {
  event.stopPropagation();
  onActivate?.();
}

function stopGraphNodePointerDown(event: PointerEvent<HTMLDivElement>) {
  if ((event.target as HTMLElement).closest(GRAPH_MOVE_HANDLE_SELECTOR)) return;
  event.stopPropagation();
}

function activateGraphNodeFromKeyboard(
  event: KeyboardEvent<HTMLDivElement>,
  onActivate?: () => void,
) {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  event.stopPropagation();
  onActivate?.();
}

function GraphNodeMoveHandle({ data }: { data: GraphNodeData }) {
  if (!data.moveHandleLabel) return null;
  return (
    <button
      type="button"
      className={GRAPH_MOVE_HANDLE_CLASS}
      aria-label={data.moveHandleLabel}
      aria-pressed={data.movePressed || undefined}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={data.onMoveKeyDown}
    >
      <span aria-hidden="true">↕</span>
    </button>
  );
}

function GraphNodeTaskMeta({ data }: { data: GraphNodeData }) {
  if (data.kind !== "task") return null;
  return (
    <div className="wsv-graph-node__meta">
      <span className={`wsv-priority wsv-priority--${data.priority}`}>
        {data.priority}
      </span>
      <span>{data.status}</span>
    </div>
  );
}

function GraphNodeOutcome({ outcome }: { outcome: NonNullable<GraphNodeData["outcome"]> }) {
  return (
    <dl className="wsv-graph-node__outcome">
      <div>
        <dt>Progress</dt>
        <dd>
          {outcome.recordedProgress === null
            ? "Unrecorded"
            : String(outcome.recordedProgress)}
        </dd>
      </div>
      {outcome.target !== null ? (
        <div>
          <dt>Target</dt>
          <dd>{outcome.target}</dd>
        </div>
      ) : null}
      {outcome.status !== null ? (
        <div>
          <dt>Status</dt>
          <dd>{outcome.status}</dd>
        </div>
      ) : null}
      <div>
        <dt>Tasks</dt>
        <dd>
          {`${outcome.linkedTotal} linked`}
          {` · ${outcome.visibleTotal} of ${outcome.linkedTotal} visible`}
        </dd>
      </div>
    </dl>
  );
}

function GraphNodeAction({ data, actionable }: { data: GraphNodeData; actionable: boolean }) {
  return (
    <div
      className="wsv-graph-node__action"
      aria-label={actionable ? graphNodeActionLabel(data) : undefined}
      onKeyDown={actionable ? (event) => activateGraphNodeFromKeyboard(event, data.onActivate) : undefined}
      role={actionable ? "button" : undefined}
      tabIndex={actionable ? 0 : undefined}
    >
      <div className="wsv-graph-node__eyebrow">{data.eyebrow}</div>
      <div className="wsv-graph-node__title" title={data.title}>
        {data.title}
      </div>
      <GraphNodeTaskMeta data={data} />
      {data.outcome ? <GraphNodeOutcome outcome={data.outcome} /> : null}
    </div>
  );
}

function GraphNodeContextBadge({ data }: { data: GraphNodeData }) {
  if (data.kind !== "task") return null;
  const empty = data.contextCount <= 0;
  return (
    <button
      type="button"
      className={[
        "wsv-context-badge",
        "wsv-graph-context-trigger",
        empty ? "wsv-graph-context-trigger--empty" : "",
        "nodrag",
        "nopan",
      ].filter(Boolean).join(" ")}
      aria-label={
        empty
          ? `Add context for task ${data.id}`
          : `Open context for task ${data.id}: ${data.contextCount} linked context items`
      }
      aria-haspopup="dialog"
      onPointerDown={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.stopPropagation();
        data.onOpenContext?.(event.currentTarget);
      }}
    >
      {empty ? "+ Context" : `↗ ${data.contextCount}`}
    </button>
  );
}

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
      className={graphNodeClassName(data)}
      onClick={actionable ? (event) => activateGraphNodeFromClick(event, data.onActivate) : undefined}
      onPointerDown={actionable ? stopGraphNodePointerDown : undefined}
    >
      {before}
      <GraphNodeMoveHandle data={data} />
      <GraphNodeAction data={data} actionable={actionable} />
      <GraphNodeContextBadge data={data} />
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

function GraphMiniMap() {
  return (
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
  );
}

const NODE_TYPES = { workspace: WorkspaceNode };
const EDGE_TYPES = { planning: PlanningEdge };

export function graphEmptyTitle(emptyKind: string) {
  if (emptyKind === "no-tasks") return "No tasks yet";
  if (emptyKind === "all-complete") return "All matching tasks are completed";
  return "No work matches these filters";
}

export function graphEmptyDetail(emptyKind: string) {
  if (emptyKind === "no-tasks") return "Create a Task to start planning this workspace.";
  if (emptyKind === "all-complete") return "Show completed tasks to bring them back into view.";
  return "Clear a filter to bring tasks and their relationships back.";
}

export function GraphLegend() {
  return (
    <div className="wsv-graph-legend" aria-label="Relationship legend">
      {Object.entries(EDGE_COLORS).map(([label, color]) => (
        <span key={label} aria-label={label}>
          <i style={{ background: color }} /> {label}
        </span>
      ))}
      {OUTCOME_LEGEND.map((entry) => (
        <span key={entry.key} aria-label={entry.accessibleName}>
          <i style={{ background: entry.color }} /> {entry.label}
        </span>
      ))}
    </div>
  );
}

export function GraphEmptyOverlay({ emptyKind }: { emptyKind: string }) {
  return (
    <div className="wsv-graph-empty-overlay" role="status">
      <strong>{graphEmptyTitle(emptyKind)}</strong>
      <span>{graphEmptyDetail(emptyKind)}</span>
    </div>
  );
}

export type GraphFlowCanvasProps = {
  modelNodeCount: number;
  nodes: WorkspaceFlowNode[];
  edges: WorkspaceFlowEdge[];
  onInit: (instance: ReactFlowInstance<WorkspaceFlowNode, WorkspaceFlowEdge>) => void;
  onNodeDragStart: (_event: unknown, node: WorkspaceFlowNode) => void;
  onNodeDrag: (_event: unknown, node: WorkspaceFlowNode) => void;
  onNodeDragStop: (_event: unknown, node: WorkspaceFlowNode) => void;
  onMoveEnd: (_event: unknown, viewport: { x: number; y: number; zoom: number }) => void;
};

export function GraphFlowCanvas({
  modelNodeCount,
  nodes,
  edges,
  onInit,
  onNodeDragStart,
  onNodeDrag,
  onNodeDragStop,
  onMoveEnd,
}: GraphFlowCanvasProps) {
  return (
    <ReactFlow<WorkspaceFlowNode, WorkspaceFlowEdge>
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      edgeTypes={EDGE_TYPES}
      onInit={onInit}
      onNodeDragStart={onNodeDragStart}
      onNodeDrag={onNodeDrag}
      onNodeDragStop={onNodeDragStop}
      onMoveEnd={onMoveEnd}
      nodeDragThreshold={GRAPH_DRAG_THRESHOLD_PX}
      nodesDraggable
      selectNodesOnDrag={false}
      nodesFocusable={false}
      nodesConnectable={false}
      elementsSelectable
      onlyRenderVisibleElements={shouldVirtualizeGraph(modelNodeCount)}
      fitView={false}
      minZoom={GRAPH_ZOOM_MIN}
      maxZoom={1.7}
      proOptions={{ hideAttribution: true }}
    >
      <Background color="var(--wsv-graph-grid)" gap={24} size={1} />
      <GraphMiniMap />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}
