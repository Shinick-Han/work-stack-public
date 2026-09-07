import { useCallback, useEffect, useRef } from "react";
import type { ReactFlowInstance } from "@xyflow/react";
import { neighborhoodLayoutReady, objectiveNeighborhoodFitView } from "./graphCamera";
import {
  clampGraphCoord,
  clampGraphZoom,
  type GraphLocalViewData,
} from "./graphViewState";
import type { WorkspaceFlowEdge, WorkspaceFlowNode } from "./graphViewTypes";
import type { LocalViewStore } from "./localViewState";

type FlowInstance = ReactFlowInstance<WorkspaceFlowNode, WorkspaceFlowEdge>;

type ViewportRefs = {
  flowInstanceRef: { current: FlowInstance | null };
  didInitialFitRef: { current: boolean };
  pendingFitRef: { current: boolean };
  ignoreMoveEndRef: { current: number };
  layoutReadyRef: { current: boolean };
  graphLocalRef: { current: GraphLocalViewData };
  modelRef: { current: { nodes: readonly WorkspaceFlowNode[] } };
  selectedObjectiveIdRef: { current: string | null };
  topologyKeyRef: { current: string | null };
  requestedTopologyKeyRef: { current: string | null };
  neighborhoodFitKeyRef: { current: string | null };
  positionedNodesRef: { current: readonly WorkspaceFlowNode[] };
  routedEdgesRef: { current: readonly WorkspaceFlowEdge[] };
};

function applySavedViewport(refs: ViewportRefs) {
  const instance = refs.flowInstanceRef.current;
  const viewport = refs.graphLocalRef.current.viewport;
  if (!instance || !viewport) return false;
  refs.ignoreMoveEndRef.current += 1;
  instance.setViewport(viewport);
  refs.didInitialFitRef.current = true;
  refs.pendingFitRef.current = false;
  return true;
}

function runFitView(refs: ViewportRefs) {
  if (!refs.flowInstanceRef.current) return;
  if (!refs.modelRef.current.nodes.length) {
    refs.pendingFitRef.current = true;
    return;
  }
  refs.ignoreMoveEndRef.current += 1;
  refs.didInitialFitRef.current = true;
  refs.pendingFitRef.current = false;
  refs.flowInstanceRef.current.fitView({ padding: 0.12, minZoom: 0.42, maxZoom: 1.15 });
}

function tryInitialFit(refs: ViewportRefs) {
  if (refs.didInitialFitRef.current) return;
  if (refs.selectedObjectiveIdRef.current) return;
  if (applySavedViewport(refs)) return;
  if (!refs.layoutReadyRef.current) return;
  if (!refs.flowInstanceRef.current) return;
  runFitView(refs);
}

function applyObjectiveNeighborhood(refs: ViewportRefs) {
  const objectiveId = refs.selectedObjectiveIdRef.current;
  if (!objectiveId) {
    refs.neighborhoodFitKeyRef.current = null;
    return false;
  }
  if (!neighborhoodLayoutReady(refs.topologyKeyRef.current, refs.requestedTopologyKeyRef.current)) {
    return false;
  }
  if (!refs.flowInstanceRef.current) return false;
  const fitKey = `${objectiveId}::${refs.topologyKeyRef.current ?? ""}`;
  if (refs.neighborhoodFitKeyRef.current === fitKey) return true;
  const options = objectiveNeighborhoodFitView(
    objectiveId,
    refs.positionedNodesRef.current,
    refs.routedEdgesRef.current,
  );
  if (!options) return false;
  refs.ignoreMoveEndRef.current += 1;
  refs.didInitialFitRef.current = true;
  refs.pendingFitRef.current = false;
  refs.neighborhoodFitKeyRef.current = fitKey;
  refs.flowInstanceRef.current.fitView(options);
  return true;
}

function syncViewport(refs: ViewportRefs) {
  if (applyObjectiveNeighborhood(refs)) return;
  tryInitialFit(refs);
}

export function persistGraphViewport(
  writeLocal: (mutator: (current: GraphLocalViewData) => GraphLocalViewData) => GraphLocalViewData,
  viewport: { x: number; y: number; zoom: number },
) {
  writeLocal((current) => ({
    ...current,
    viewport: {
      x: clampGraphCoord(viewport.x),
      y: clampGraphCoord(viewport.y),
      zoom: clampGraphZoom(viewport.zoom),
    },
  }));
}

export function useGraphViewport(args: {
  layoutTopologyKey: string | null;
  requestedTopologyKey: string | null;
  graphLocalViewport: GraphLocalViewData["viewport"];
  modelNodeCount: number;
  store: LocalViewStore<GraphLocalViewData>;
  graphLocalRef: { current: GraphLocalViewData };
  modelRef: { current: { nodes: readonly WorkspaceFlowNode[] } };
  positionedNodes: readonly WorkspaceFlowNode[];
  routedEdges: readonly WorkspaceFlowEdge[];
  selectedObjectiveId?: string | null;
  graphLocalPositions: GraphLocalViewData["positions"];
  writeLocal: (mutator: (current: GraphLocalViewData) => GraphLocalViewData) => GraphLocalViewData;
  skipExternalRef: { current: boolean };
  setGraphLocal: (data: GraphLocalViewData) => void;
  setAnnouncement: (message: string) => void;
  cancelGesture: (announce?: boolean) => void;
}) {
  const flowInstanceRef = useRef<FlowInstance | null>(null);
  const didInitialFitRef = useRef(false);
  const pendingFitRef = useRef(false);
  const ignoreMoveEndRef = useRef(0);
  const layoutReadyRef = useRef(false);
  const selectedObjectiveIdRef = useRef<string | null>(null);
  const topologyKeyRef = useRef<string | null>(null);
  const neighborhoodFitKeyRef = useRef<string | null>(null);
  const pendingResetFitRef = useRef(false);
  const positionedNodesRef = useRef(args.positionedNodes);
  const routedEdgesRef = useRef(args.routedEdges);
  const requestedTopologyKeyRef = useRef<string | null>(null);
  layoutReadyRef.current = neighborhoodLayoutReady(args.layoutTopologyKey, args.requestedTopologyKey);
  selectedObjectiveIdRef.current = args.selectedObjectiveId ?? null;
  topologyKeyRef.current = args.layoutTopologyKey;
  requestedTopologyKeyRef.current = args.requestedTopologyKey;
  positionedNodesRef.current = args.positionedNodes;
  routedEdgesRef.current = args.routedEdges;
  const refs = useRef<ViewportRefs>(null!);
  refs.current = {
    flowInstanceRef, didInitialFitRef, pendingFitRef, ignoreMoveEndRef,
    layoutReadyRef, graphLocalRef: args.graphLocalRef, modelRef: args.modelRef,
    selectedObjectiveIdRef, topologyKeyRef, requestedTopologyKeyRef,
    neighborhoodFitKeyRef,
    positionedNodesRef, routedEdgesRef,
  };
  const applySaved = useCallback(() => applySavedViewport(refs.current), []);
  const initialFit = useCallback(() => syncViewport(refs.current), [applySaved]);
  useEffect(() => { initialFit(); }, [args.layoutTopologyKey, args.requestedTopologyKey, args.selectedObjectiveId, initialFit, args.graphLocalViewport]);
  useEffect(() => {
    if (pendingFitRef.current && args.modelNodeCount) initialFit();
  }, [args.modelNodeCount, initialFit]);
  useEffect(() => {
    didInitialFitRef.current = false;
    neighborhoodFitKeyRef.current = null;
    if (args.graphLocalRef.current.viewport && !selectedObjectiveIdRef.current) applySaved();
    else initialFit();
  }, [args.store, applySaved, initialFit, args.graphLocalRef]);
  useEffect(() => {
    if (!pendingResetFitRef.current) return;
    if (!neighborhoodLayoutReady(args.layoutTopologyKey, args.requestedTopologyKey)) return;
    pendingResetFitRef.current = false;
    if (didInitialFitRef.current) return;
    initialFit();
  }, [args.graphLocalPositions, args.layoutTopologyKey, args.requestedTopologyKey, args.selectedObjectiveId, initialFit]);
  const persistViewport = useCallback((viewport: { x: number; y: number; zoom: number }) => {
    persistGraphViewport(args.writeLocal, viewport);
  }, [args.writeLocal]);
  const resetGraphLayout = useCallback(() => {
    args.cancelGesture(false);
    args.skipExternalRef.current = true;
    const next = args.store.reset();
    args.skipExternalRef.current = false;
    args.setGraphLocal(next);
    args.setAnnouncement("Graph layout reset");
    neighborhoodFitKeyRef.current = null;
    didInitialFitRef.current = false;
    pendingResetFitRef.current = true;
  }, [args.cancelGesture, args.store, args.skipExternalRef, args.setGraphLocal, args.setAnnouncement]);
  const onMoveEnd = useCallback((_event: unknown, viewport: { x: number; y: number; zoom: number }) => {
    if (ignoreMoveEndRef.current > 0) {
      ignoreMoveEndRef.current -= 1;
      return;
    }
    persistViewport(viewport);
  }, [persistViewport]);
  const onInit = useCallback((instance: FlowInstance) => {
    flowInstanceRef.current = instance;
    initialFit();
  }, [initialFit]);
  return { onInit, onMoveEnd, resetGraphLayout };
}
