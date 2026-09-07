import { useEffect, useMemo, useRef, useState } from "react";
import {
  applySavedPositions,
  manualPositionIds,
  shouldDropElkRoute,
  type GraphLocalViewData,
} from "./graphViewState";
import { layoutPlanningGraph, planningGraphTopologyKey, type GraphEdgeRoute } from "./graphLayout";
import { makeGraphModel } from "./graphViewModel";
import type { GraphMoveGesture, GraphViewProps, GraphEdgeData, WorkspaceFlowEdge, WorkspaceFlowNode } from "./graphViewTypes";
import type { GraphKeyResultSelection } from "./graphViewTypes";
import type { KeyResultProjection, OutcomeFilter } from "./keyResultModel";
import type { WorkspaceEdge, WorkspaceNote, WorkspaceObjective, WorkspaceTask } from "./types";

type GraphLayoutState = {
  topologyKey: string | null;
  nodes: WorkspaceFlowNode[];
  edgeRoutes: Record<string, GraphEdgeRoute>;
};

function overlayGesturePosition(
  nodes: WorkspaceFlowNode[],
  gesture: GraphMoveGesture | null,
  persistWorkspaceId: string,
) {
  if (!gesture || gesture.workspaceId !== persistWorkspaceId) return nodes;
  return nodes.map((node) => (
    node.id === gesture.id ? { ...node, position: gesture.current } : node
  ));
}

function routeGraphEdges(
  edges: readonly WorkspaceFlowEdge[],
  routesById: Record<string, GraphEdgeRoute>,
  manualIds: ReadonlySet<string>,
): WorkspaceFlowEdge[] {
  return edges.map((edge) => ({
    ...edge,
    data: {
      ...edge.data,
      route: shouldDropElkRoute(edge.source, edge.target, manualIds)
        ? undefined
        : routesById[edge.id],
    } as GraphEdgeData,
  }));
}

export function catalogNodeIds(args: {
  canonicalTasks: readonly WorkspaceTask[];
  objectives: readonly WorkspaceObjective[];
  notes: readonly WorkspaceNote[];
  edges: readonly WorkspaceEdge[];
  keyResultProjection: KeyResultProjection | null;
  outcome: OutcomeFilter | null;
}) {
  const catalog = makeGraphModel(
    args.canonicalTasks,
    args.objectives,
    args.notes,
    args.edges,
    null,
    null,
    args.keyResultProjection,
    args.outcome,
  );
  return new Set(catalog.nodes.map((node) => node.id));
}

export function useGraphLayout(args: {
  tasks: readonly WorkspaceTask[];
  objectives: GraphViewProps["objectives"];
  notes: readonly WorkspaceNote[];
  edges: readonly WorkspaceEdge[];
  selectedTaskId?: string | null;
  selectedObjectiveId?: string | null;
  keyResultProjection: KeyResultProjection | null;
  outcome: OutcomeFilter | null;
  selectedNoteId: string | null;
  graphKeyResult: GraphKeyResultSelection | null;
  canonicalTasks: readonly WorkspaceTask[];
  graphLocal: GraphLocalViewData;
  gesture: GraphMoveGesture | null;
  persistWorkspaceId: string;
  catalogRef: { current: ReadonlySet<string> };
}) {
  const model = useMemo(
    () => makeGraphModel(
      args.tasks,
      args.objectives,
      args.notes,
      args.edges,
      args.selectedTaskId,
      args.selectedObjectiveId,
      args.keyResultProjection,
      args.outcome,
      args.selectedNoteId,
      args.graphKeyResult,
    ),
    [
      args.tasks, args.objectives, args.notes, args.edges, args.selectedTaskId,
      args.selectedObjectiveId, args.keyResultProjection, args.outcome,
      args.selectedNoteId, args.graphKeyResult,
    ],
  );
  const catalogIds = useMemo(
    () => catalogNodeIds(args),
    [
      args.canonicalTasks, args.objectives, args.notes, args.edges,
      args.keyResultProjection, args.outcome,
    ],
  );
  args.catalogRef.current = catalogIds;
  const topologyKey = useMemo(
    () => planningGraphTopologyKey(model.nodes, model.edges),
    [model],
  );
  const modelRef = useRef(model);
  modelRef.current = model;
  const [layout, setLayout] = useState<GraphLayoutState>({
    topologyKey: null, nodes: [], edgeRoutes: {},
  });
  useEffect(() => {
    const { nodes, edges: modelEdges } = modelRef.current;
    let cancelled = false;
    void layoutPlanningGraph(nodes, modelEdges).then((result) => {
      if (!cancelled) {
        setLayout({ topologyKey, nodes: result.nodes, edgeRoutes: result.edgeRoutes });
      }
    }).catch(() => {
      if (!cancelled) setLayout({ topologyKey, nodes, edgeRoutes: {} });
    });
    return () => { cancelled = true; };
  }, [topologyKey]);
  const positionsById = useMemo(() => new Map(
    layout.nodes.map((node) => [node.id, node]),
  ), [layout]);
  const positionedNodes = useMemo(() => {
    const elkNodes = model.nodes.map((node) => {
      const laid = positionsById.get(node.id);
      return laid ? { ...node, position: laid.position } : node;
    });
    return overlayGesturePosition(
      applySavedPositions(elkNodes, args.graphLocal.positions),
      args.gesture,
      args.persistWorkspaceId,
    );
  }, [model.nodes, positionsById, args.graphLocal.positions, args.gesture, args.persistWorkspaceId]);
  const manualIds = useMemo(
    () => manualPositionIds(
      args.graphLocal.positions,
      args.gesture?.workspaceId === args.persistWorkspaceId ? args.gesture.id : null,
    ),
    [args.graphLocal.positions, args.gesture, args.persistWorkspaceId],
  );
  const routedEdges = useMemo(
    () => routeGraphEdges(model.edges, layout.edgeRoutes, manualIds),
    [model.edges, layout.edgeRoutes, manualIds],
  );
  return { model, catalogIds, modelRef, layout, positionedNodes, routedEdges, topologyKey };
}
