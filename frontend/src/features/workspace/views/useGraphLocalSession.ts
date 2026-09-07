import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  EMPTY_GRAPH_LOCAL_VIEW,
  parseGraphLocalViewData,
  pruneGraphLocalViewData,
  type GraphLocalViewData,
} from "./graphViewState";
import type { GraphMoveGesture, WorkspaceFlowNode } from "./graphViewTypes";
import {
  beginGraphGesture,
  cancelGraphGesture,
  commitGraphGesture,
  handleGraphNodeDrag,
  handleGraphNodeDragStart,
  handleGraphNodeDragStop,
  handleMoveKeyDown,
  liveGestureForWorkspace,
  type GraphGestureApi,
} from "./graphMoveSession";
import {
  createExternalUpdateGate,
  createLocalViewStore,
  LAYOUT_SAVE_FAILURE_MESSAGE,
  type LocalViewStore,
} from "./localViewState";

function useGraphStore(persistWorkspaceId: string) {
  const skipExternalRef = useRef(false);
  const [announcement, setAnnouncement] = useState("");
  const announcePersistFailure = useCallback(() => {
    setAnnouncement(LAYOUT_SAVE_FAILURE_MESSAGE);
  }, []);
  const announcePersistFailureRef = useRef(announcePersistFailure);
  announcePersistFailureRef.current = announcePersistFailure;
  const store = useMemo(
    () =>
      createLocalViewStore({
        workspaceId: persistWorkspaceId,
        view: "graph" as const,
        defaultData: EMPTY_GRAPH_LOCAL_VIEW,
        parseData: parseGraphLocalViewData,
        onPersistFailure: () => {
          queueMicrotask(() => announcePersistFailureRef.current());
        },
      }),
    [persistWorkspaceId],
  );
  const [graphLocal, setGraphLocal] = useState<GraphLocalViewData>(() => store.read());
  const graphLocalRef = useRef(graphLocal);
  graphLocalRef.current = graphLocal;
  const gateRef = useRef(createExternalUpdateGate<GraphLocalViewData>((data) => {
    setGraphLocal(data);
    setAnnouncement("Graph layout updated from another tab.");
  }));
  useEffect(() => {
    setGraphLocal(store.read());
    gateRef.current = createExternalUpdateGate<GraphLocalViewData>((data) => {
      setGraphLocal(data);
      setAnnouncement("Graph layout updated from another tab.");
    });
    const stop = store.subscribe((data) => {
      if (skipExternalRef.current) return;
      gateRef.current.onExternal(data);
    });
    return () => {
      gateRef.current.endGesture();
      stop();
    };
  }, [store]);
  return {
    store, skipExternalRef, announcement, setAnnouncement, graphLocal, setGraphLocal,
    graphLocalRef, gateRef,
  };
}

function useGraphGestureEscape(gesture: GraphMoveGesture | null, apiRef: { current: GraphGestureApi }) {
  useEffect(() => {
    if (!gesture) return;
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      cancelGraphGesture(apiRef.current);
    };
    const onPointerCancel = () => {
      if (apiRef.current.gestureRef.current?.source === "pointer") {
        cancelGraphGesture(apiRef.current);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointercancel", onPointerCancel);
    window.addEventListener("lostpointercapture", onPointerCancel);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointercancel", onPointerCancel);
      window.removeEventListener("lostpointercapture", onPointerCancel);
    };
  }, [gesture, apiRef]);
}

export function useGraphLocalSession(persistWorkspaceId: string) {
  const catalogRef = useRef<ReadonlySet<string>>(new Set());
  const local = useGraphStore(persistWorkspaceId);
  const storeRef = useRef<LocalViewStore<GraphLocalViewData>>(local.store);
  const [gesture, setGesture] = useState<GraphMoveGesture | null>(null);
  const gestureRef = useRef<GraphMoveGesture | null>(null);
  const liveGesture = liveGestureForWorkspace(gesture, persistWorkspaceId);
  gestureRef.current = liveGesture;
  if (storeRef.current !== local.store) {
    local.gateRef.current.endGesture();
    setGesture(null);
    storeRef.current = local.store;
    local.setGraphLocal(local.store.read());
  }
  const writeLocal = useCallback((mutator: (current: GraphLocalViewData) => GraphLocalViewData) => {
    local.skipExternalRef.current = true;
    const next = local.store.update((current) => {
      const proposed = mutator(current);
      return pruneGraphLocalViewData(proposed, catalogRef.current) ?? proposed;
    });
    local.skipExternalRef.current = false;
    local.setGraphLocal(next);
    return next;
  }, [local.store, local.setGraphLocal, local.skipExternalRef]);
  const apiRef = useRef<GraphGestureApi>(null!);
  apiRef.current = {
    persistWorkspaceId, gestureRef, gateRef: local.gateRef, setGesture,
    setAnnouncement: local.setAnnouncement, writeLocal,
  };
  const cancelGesture = useCallback((announce = true) => {
    cancelGraphGesture(apiRef.current, announce);
  }, []);
  const commitGesture = useCallback((position: { x: number; y: number }, active: GraphMoveGesture) => {
    commitGraphGesture(apiRef.current, position, active, catalogRef.current);
  }, [writeLocal]);
  const beginGesture = useCallback((
    node: WorkspaceFlowNode,
    source: GraphMoveGesture["source"],
    position = node.position,
  ) => beginGraphGesture(apiRef.current, node, source, position), [persistWorkspaceId]);
  const onMoveKeyDown = useCallback((node: WorkspaceFlowNode, event: Parameters<typeof handleMoveKeyDown>[1]) => {
    handleMoveKeyDown(node, event, apiRef.current, catalogRef.current);
  }, [persistWorkspaceId, writeLocal]);
  useGraphGestureEscape(gesture, apiRef);
  useEffect(() => () => {
    local.gateRef.current.endGesture();
    gestureRef.current = null;
    setGesture(null);
  }, [local.store]);
  return {
    ...local, catalogRef, liveGesture, gesture, gestureRef, writeLocal,
    cancelGesture, commitGesture, beginGesture, onMoveKeyDown,
    onNodeDragStart: (_event: unknown, node: WorkspaceFlowNode) => handleGraphNodeDragStart(node, apiRef.current),
    onNodeDrag: (_event: unknown, node: WorkspaceFlowNode) => handleGraphNodeDrag(node, apiRef.current),
    onNodeDragStop: (_event: unknown, node: WorkspaceFlowNode) => {
      handleGraphNodeDragStop(node, apiRef.current, catalogRef.current);
    },
  };
}
