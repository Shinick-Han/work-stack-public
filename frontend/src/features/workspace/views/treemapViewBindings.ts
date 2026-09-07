import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { KeyResultProjection } from "./keyResultModel";
import { buildOutcomeTreemap } from "./keyResultViewModel";
import {
  createExternalUpdateGate,
  createLocalViewStore,
  LAYOUT_SAVE_FAILURE_MESSAGE,
  type LocalViewStore,
} from "./localViewState";
import type { TreemapGesture, TreemapMoveItem } from "./treemapMove";
import {
  applyPreviewToGroups,
  applyStoredTreemapOrders,
  collectTreemapCatalog,
  formatTreemapPosition,
  previewMove,
  splitLegacyReservedGroups,
  visibleIdsForScope,
  type TreemapOrderNode,
} from "./treemapOrdering";
import type { TreemapPointerApi } from "./treemapPointerController";
import {
  EMPTY_TREEMAP_LOCAL_VIEW,
  commitVisibleTreemapReorder,
  parseTreemapLocalViewData,
  pruneTreemapLocalViewData,
  type TreemapLocalViewData,
  type TreemapMode,
} from "./treemapViewState";
import type { WorkspaceObjective, WorkspaceTask } from "./types";
import { buildTreemapGroups } from "./viewModels";

export function treemapMode(keyResultProjection?: KeyResultProjection): TreemapMode {
  return keyResultProjection ? "outcome" : "legacy";
}

export function buildPresentationGroups(
  tasks: readonly WorkspaceTask[],
  objectives: readonly WorkspaceObjective[],
  keyResultProjection?: KeyResultProjection,
): TreemapOrderNode[] {
  const legacy = buildTreemapGroups(tasks, objectives);
  if (!keyResultProjection) return splitLegacyReservedGroups(legacy as TreemapOrderNode[]);
  const leafByTaskId = new Map(
    legacy.flatMap((group) => group.children.map((leaf) => [leaf.taskId, leaf] as const)),
  );
  return buildOutcomeTreemap(
    keyResultProjection,
    tasks,
    (task) => leafByTaskId.get(task.id)?.size ?? 1,
  ).map((objectiveNode) => ({
    name: objectiveNode.name,
    nodeKind: objectiveNode.nodeKind,
    groupKey: objectiveNode.key,
    objectiveId: objectiveNode.objectiveId,
    children: objectiveNode.children.map((group) => ({
      name: group.name,
      nodeKind: group.nodeKind,
      groupKey: group.key,
      bucket: group.bucket,
      objectiveId: group.objectiveId ?? objectiveNode.objectiveId,
      children: group.children
        .map((leaf) => leafByTaskId.get(leaf.id))
        .filter((leaf): leaf is NonNullable<typeof leaf> => Boolean(leaf))
        .map((leaf) => ({ ...leaf, nodeKind: "task" as const })),
    })).filter((group) => group.children.length > 0),
  })).filter((objectiveNode) => objectiveNode.children.length > 0);
}

export function useTreemapStore(workspaceId: string) {
  const persistWorkspaceId = workspaceId;
  const skipExternalRef = useRef(false);
  const [announcement, setAnnouncement] = useState("");
  const store = useMemo(
    () =>
      createLocalViewStore({
        workspaceId: persistWorkspaceId,
        view: "treemap" as const,
        defaultData: EMPTY_TREEMAP_LOCAL_VIEW,
        parseData: parseTreemapLocalViewData,
        onPersistFailure: () => setAnnouncement(LAYOUT_SAVE_FAILURE_MESSAGE),
      }),
    [persistWorkspaceId],
  );
  const storeRef = useRef<LocalViewStore<TreemapLocalViewData>>(store);
  const [local, setLocal] = useState<TreemapLocalViewData>(() => store.read());
  if (storeRef.current !== store) {
    storeRef.current = store;
    setLocal(store.read());
  }
  const gateRef = useRef(createExternalUpdateGate<TreemapLocalViewData>((data) => {
    setLocal(data);
    setAnnouncement("Treemap order updated from another tab.");
  }));
  useEffect(() => {
    setLocal(store.read());
    gateRef.current = createExternalUpdateGate<TreemapLocalViewData>((data) => {
      setLocal(data);
      setAnnouncement("Treemap order updated from another tab.");
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
  return { store, storeRef, local, setLocal, gateRef, skipExternalRef, announcement, setAnnouncement };
}

export function useTreemapProjection(input: {
  tasks: readonly WorkspaceTask[];
  referenceTasks?: readonly WorkspaceTask[];
  objectives: readonly WorkspaceObjective[];
  keyResultProjection?: KeyResultProjection;
}) {
  const mode = treemapMode(input.keyResultProjection);
  const canonicalTasks = input.referenceTasks ?? input.tasks;
  const visibleGroups = useMemo(
    () => buildPresentationGroups(input.tasks, input.objectives, input.keyResultProjection),
    [input.tasks, input.objectives, input.keyResultProjection],
  );
  const catalogGroups = useMemo(
    () => buildPresentationGroups(canonicalTasks, input.objectives, input.keyResultProjection),
    [canonicalTasks, input.objectives, input.keyResultProjection],
  );
  const catalog = useMemo(() => collectTreemapCatalog(catalogGroups, mode), [catalogGroups, mode]);
  const catalogRef = useRef(catalog);
  catalogRef.current = catalog;
  const visibleRef = useRef(visibleGroups);
  visibleRef.current = visibleGroups;
  const modeRef = useRef(mode);
  modeRef.current = mode;
  return { mode, visibleGroups, catalogRef, visibleRef, modeRef };
}

export function useTreemapReorder(
  store: LocalViewStore<TreemapLocalViewData>,
  setLocal: (data: TreemapLocalViewData) => void,
  skipExternalRef: { current: boolean },
  catalogRef: { current: Map<string, string[]> },
  modeRef: { current: TreemapMode },
) {
  return useCallback((
    scope: string,
    visibleIds: readonly string[],
    nextVisibleIds: readonly string[],
  ) => {
    skipExternalRef.current = true;
    const next = store.update((current) => {
      const catalogIds = catalogRef.current.get(scope) ?? visibleIds;
      const committed = commitVisibleTreemapReorder(
        current,
        scope,
        catalogIds,
        visibleIds,
        nextVisibleIds,
        catalogRef.current,
        modeRef.current,
      );
      return committed ?? pruneTreemapLocalViewData(current, catalogRef.current, modeRef.current);
    });
    skipExternalRef.current = false;
    setLocal(next);
    return next;
  }, [catalogRef, modeRef, setLocal, skipExternalRef, store]);
}

export function useTreemapOrderedGroups(
  visibleGroups: readonly TreemapOrderNode[],
  local: TreemapLocalViewData,
  mode: TreemapMode,
  gesture: TreemapGesture | null,
) {
  return useMemo(() => {
    const applied = applyStoredTreemapOrders(visibleGroups, local, mode);
    if (gesture?.source !== "keyboard") return applied;
    const preview = previewMove(gesture.visibleIds, gesture.originIndex, gesture.currentIndex);
    return applyPreviewToGroups(applied, gesture.scope, preview, mode);
  }, [visibleGroups, local, mode, gesture]);
}

type TreemapGate = {
  beginGesture: () => void;
  cancelGesture: () => unknown;
  endGesture: () => void;
};

type GestureActionOptions = {
  writeReorder: (
    scope: string,
    visibleIds: readonly string[],
    nextVisibleIds: readonly string[],
  ) => TreemapLocalViewData;
  gateRef: { current: TreemapGate };
  setAnnouncement: (message: string) => void;
  visibleRef: { current: readonly TreemapOrderNode[] };
  modeRef: { current: TreemapMode };
  pointerApiRef: { current: TreemapPointerApi };
};

export function useTreemapGestureActions(options: GestureActionOptions) {
  const {
    writeReorder, gateRef, setAnnouncement, visibleRef, modeRef, pointerApiRef,
  } = options;
  const [gesture, setGesture] = useState<TreemapGesture | null>(null);
  const gestureRef = useRef<TreemapGesture | null>(null);
  const cancelGesture = useCallback((announce = true, message = "Move cancelled") => {
    const active = gestureRef.current;
    pointerApiRef.current.closeCapture();
    gateRef.current.cancelGesture();
    setGesture(null);
    gestureRef.current = null;
    if (announce && active && (active.activated || active.source === "keyboard")) {
      setAnnouncement(message);
    }
  }, [gateRef, pointerApiRef, setAnnouncement]);
  const commitGesture = useCallback((active: TreemapGesture, nextIndex: number) => {
    const nextVisible = previewMove(active.visibleIds, active.originIndex, nextIndex);
    gateRef.current.endGesture();
    if (nextVisible.join("\0") !== active.visibleIds.join("\0")) {
      writeReorder(active.scope, active.visibleIds, nextVisible);
    }
    setGesture(null);
    gestureRef.current = null;
    setAnnouncement(
      `Moved ${active.kind} ${active.id} to position ${formatTreemapPosition(nextIndex, active.visibleIds.length)}`,
    );
  }, [gateRef, setAnnouncement, writeReorder]);
  const beginTracked = useCallback((
    item: TreemapMoveItem,
    source: TreemapGesture["source"],
    point: { x: number; y: number },
  ) => {
    const visibleIds = visibleIdsForScope(visibleRef.current, item.scope, modeRef.current);
    const originIndex = visibleIds.indexOf(item.id);
    if (originIndex < 0) return null;
    const next: TreemapGesture = {
      ...item,
      source,
      visibleIds,
      originIndex,
      currentIndex: originIndex,
      startX: point.x,
      startY: point.y,
      activated: source === "keyboard",
    };
    gestureRef.current = next;
    if (source === "keyboard") {
      setGesture(next);
      gateRef.current.beginGesture();
      setAnnouncement(`Picked up ${item.kind} ${item.id}`);
    }
    return next;
  }, [gateRef, modeRef, setAnnouncement, visibleRef]);
  return { gesture, setGesture, gestureRef, cancelGesture, commitGesture, beginTracked };
}

export function useTreemapReset(
  cancelGesture: (announce?: boolean) => void,
  store: LocalViewStore<TreemapLocalViewData>,
  setLocal: (data: TreemapLocalViewData) => void,
  skipExternalRef: { current: boolean },
  setAnnouncement: (message: string) => void,
) {
  return useCallback(() => {
    cancelGesture(false);
    skipExternalRef.current = true;
    const next = store.reset();
    skipExternalRef.current = false;
    setLocal(next);
    setAnnouncement("Treemap order reset");
  }, [cancelGesture, setAnnouncement, setLocal, skipExternalRef, store]);
}

export function pressedMoveId(gesture: TreemapGesture | null): string | null {
  return gesture && (gesture.activated || gesture.source === "keyboard")
    ? gesture.id
    : null;
}
