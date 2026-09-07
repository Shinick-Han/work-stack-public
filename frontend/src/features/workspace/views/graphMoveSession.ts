import type { KeyboardEvent, MutableRefObject } from "react";
import {
  arrowMoveDelta,
  clampGraphCoord,
  formatGraphCoordinates,
  upsertGraphPosition,
  type GraphLocalViewData,
} from "./graphViewState";
import type { GraphMoveGesture, WorkspaceFlowNode } from "./graphViewTypes";
import { createExternalUpdateGate } from "./localViewState";

type GraphUpdateGate = ReturnType<typeof createExternalUpdateGate<GraphLocalViewData>>;

export type GraphGestureApi = {
  persistWorkspaceId: string;
  gestureRef: MutableRefObject<GraphMoveGesture | null>;
  gateRef: MutableRefObject<GraphUpdateGate>;
  setGesture: (gesture: GraphMoveGesture | null) => void;
  setAnnouncement: (message: string) => void;
  writeLocal: (mutator: (current: GraphLocalViewData) => GraphLocalViewData) => GraphLocalViewData;
};

export function moveLabelId(data: WorkspaceFlowNode["data"]) {
  if (data.kind === "key-result" && data.outcome) {
    return `${data.outcome.objectiveId} ${data.outcome.keyResultId}`;
  }
  return data.id;
}

export function liveGestureForWorkspace(
  gesture: GraphMoveGesture | null,
  persistWorkspaceId: string,
) {
  return gesture?.workspaceId === persistWorkspaceId ? gesture : null;
}

export function cancelGraphGesture(api: GraphGestureApi, announce = true) {
  const active = api.gestureRef.current;
  api.gateRef.current.cancelGesture();
  api.setGesture(null);
  api.gestureRef.current = null;
  if (active && announce) api.setAnnouncement("Move cancelled");
}

export function commitGraphGesture(
  api: GraphGestureApi,
  position: { x: number; y: number },
  active: GraphMoveGesture,
  catalogIds: ReadonlySet<string>,
) {
  api.gateRef.current.endGesture();
  const next = api.writeLocal((current) => {
    const updated = upsertGraphPosition(
      current,
      active.id,
      position.x,
      position.y,
      catalogIds,
    );
    return updated ?? current;
  });
  api.setGesture(null);
  api.gestureRef.current = null;
  const saved = next.positions.find((item) => item.id === active.id);
  api.setAnnouncement(
    `Moved ${active.kind} ${active.labelId} to ${formatGraphCoordinates(saved?.x ?? position.x, saved?.y ?? position.y)}`,
  );
}

export function beginGraphGesture(
  api: GraphGestureApi,
  node: WorkspaceFlowNode,
  source: GraphMoveGesture["source"],
  position = node.position,
) {
  const next: GraphMoveGesture = {
    id: node.id,
    kind: node.data.kind,
    labelId: moveLabelId(node.data),
    workspaceId: api.persistWorkspaceId,
    origin: { ...position },
    current: { ...position },
    source,
  };
  api.gestureRef.current = next;
  api.setGesture(next);
  api.gateRef.current.beginGesture();
  api.setAnnouncement(`Picked up ${next.kind} ${next.labelId}`);
}

function confirmKeyboardMove(
  event: KeyboardEvent<HTMLButtonElement>,
  node: WorkspaceFlowNode,
  active: GraphMoveGesture | null,
  api: GraphGestureApi,
  catalogIds: ReadonlySet<string>,
) {
  event.preventDefault();
  event.stopPropagation();
  if (active?.source === "pointer") return;
  if (!active || active.id !== node.id) {
    if (active) cancelGraphGesture(api, false);
    beginGraphGesture(api, node, "keyboard");
    return;
  }
  commitGraphGesture(api, active.current, active, catalogIds);
}

function nudgeOrEscapeKeyboardMove(
  event: KeyboardEvent<HTMLButtonElement>,
  active: GraphMoveGesture,
  api: GraphGestureApi,
) {
  if (event.key === "Escape") {
    event.preventDefault();
    cancelGraphGesture(api);
    return;
  }
  const delta = arrowMoveDelta(event);
  if (!delta) return;
  event.preventDefault();
  const current = {
    x: clampGraphCoord(active.current.x + delta.x),
    y: clampGraphCoord(active.current.y + delta.y),
  };
  const next = { ...active, current };
  api.gestureRef.current = next;
  api.setGesture(next);
  api.setAnnouncement(
    `${active.kind} ${active.labelId} at ${formatGraphCoordinates(current.x, current.y)}`,
  );
}

export function handleMoveKeyDown(
  node: WorkspaceFlowNode,
  event: KeyboardEvent<HTMLButtonElement>,
  api: GraphGestureApi,
  catalogIds: ReadonlySet<string>,
) {
  const active = liveGestureForWorkspace(api.gestureRef.current, api.persistWorkspaceId);
  if (event.key === "Enter" || event.key === " ") {
    confirmKeyboardMove(event, node, active, api, catalogIds);
    return;
  }
  if (!active || active.id !== node.id || active.source !== "keyboard") {
    if (event.key === "Escape" && active) {
      event.preventDefault();
      cancelGraphGesture(api);
    }
    return;
  }
  nudgeOrEscapeKeyboardMove(event, active, api);
}

export function handleGraphNodeDragStart(
  node: WorkspaceFlowNode,
  api: GraphGestureApi,
) {
  const active = liveGestureForWorkspace(api.gestureRef.current, api.persistWorkspaceId);
  if (active?.source === "keyboard") cancelGraphGesture(api, false);
  beginGraphGesture(api, node, "pointer", node.position);
}

export function handleGraphNodeDrag(
  node: WorkspaceFlowNode,
  api: GraphGestureApi,
) {
  const active = api.gestureRef.current;
  if (!active || active.workspaceId !== api.persistWorkspaceId || active.id !== node.id || active.source !== "pointer") {
    return;
  }
  const current = {
    x: clampGraphCoord(node.position.x),
    y: clampGraphCoord(node.position.y),
  };
  const next = { ...active, current };
  api.gestureRef.current = next;
  api.setGesture(next);
}

export function handleGraphNodeDragStop(
  node: WorkspaceFlowNode,
  api: GraphGestureApi,
  catalogIds: ReadonlySet<string>,
) {
  const active = api.gestureRef.current;
  if (!active || active.workspaceId !== api.persistWorkspaceId || active.id !== node.id || active.source !== "pointer") {
    return;
  }
  commitGraphGesture(api, node.position, active, catalogIds);
}
