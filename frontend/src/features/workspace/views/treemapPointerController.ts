import {
  useCallback,
  useRef,
  type MutableRefObject,
  type PointerEvent as ReactPointerEvent,
} from "react";

import type { TreemapGesture, TreemapMoveItem } from "./treemapMove";
import { TREEMAP_DRAG_THRESHOLD_PX } from "./treemapViewState";

export type TreemapPointerApi = {
  closeCapture: () => void;
  detachPointer: () => void;
};

type BeginTracked = (
  item: TreemapMoveItem,
  source: TreemapGesture["source"],
  point: { x: number; y: number },
) => TreemapGesture | null;

type CancelGesture = (announce?: boolean, message?: string) => void;

type CommitGesture = (active: TreemapGesture, nextIndex: number) => void;

export function dropTargetFrom(event: { target: EventTarget | null }): TreemapMoveItem | null {
  const node = event.target instanceof Element
    ? event.target.closest("[data-treemap-item]")
    : null;
  if (!node) return null;
  const scope = node.getAttribute("data-treemap-scope");
  const id = node.getAttribute("data-treemap-id");
  const kind = node.getAttribute("data-treemap-kind");
  if (!scope || !id || (kind !== "objective" && kind !== "task")) return null;
  return { kind, id, scope };
}

export function dropTargetFromPoint(
  clientX: number,
  clientY: number,
  ignoreId?: string,
): TreemapMoveItem | null {
  if (typeof document === "undefined") return null;
  const stack: Array<Element | null> = [];
  if (typeof document.elementsFromPoint === "function") {
    stack.push(...document.elementsFromPoint(clientX, clientY));
  }
  if (typeof document.elementFromPoint === "function") {
    stack.push(document.elementFromPoint(clientX, clientY));
  }
  for (const node of stack) {
    const item = dropTargetFrom({ target: node });
    if (!item) continue;
    if (ignoreId && item.id === ignoreId) continue;
    return item;
  }
  return null;
}

export function capturePointer(target: EventTarget | null, pointerId: number) {
  if (!(target instanceof Element) || typeof target.setPointerCapture !== "function") return false;
  try {
    target.setPointerCapture(pointerId);
    return typeof target.hasPointerCapture !== "function" || target.hasPointerCapture(pointerId);
  } catch {
    return false;
  }
}

export function releasePointer(target: EventTarget | null, pointerId: number) {
  if (!(target instanceof Element) || typeof target.releasePointerCapture !== "function") return;
  try {
    if (typeof target.hasPointerCapture === "function" && !target.hasPointerCapture(pointerId)) return;
    target.releasePointerCapture(pointerId);
  } catch {
    // Capture may already have been released.
  }
}

export function applyPointerHover(
  hovered: TreemapMoveItem,
  active: TreemapGesture | null,
): TreemapGesture | null {
  if (!active || active.source !== "pointer" || hovered.scope !== active.scope) return null;
  const nextIndex = active.visibleIds.indexOf(hovered.id);
  if (nextIndex < 0) return null;
  return { ...active, currentIndex: nextIndex };
}

export function shouldActivatePointer(
  active: TreemapGesture,
  clientX: number,
  clientY: number,
): boolean {
  if (active.activated) return false;
  const distance = Math.hypot(clientX - active.startX, clientY - active.startY);
  return !(distance < TREEMAP_DRAG_THRESHOLD_PX);
}

export function shouldCancelLostPointerCapture(
  finishing: boolean,
  active: TreemapGesture | null,
): boolean {
  return !finishing && active?.source === "pointer";
}

export function resolveTreemapPointerUp(
  active: TreemapGesture | null,
  target: TreemapMoveItem | null,
  current: TreemapGesture | null,
  cancelGesture: CancelGesture,
  commitGesture: CommitGesture,
  clearUnactivated: () => void,
): void {
  if (!active || active.source !== "pointer") return;
  if (!active.activated) {
    clearUnactivated();
    return;
  }
  if (!target) {
    cancelGesture();
    return;
  }
  if (target.scope !== active.scope) {
    cancelGesture(true, "Cannot move across groups");
    return;
  }
  const nextIndex = (current ?? active).visibleIds.indexOf(target.id);
  if (nextIndex < 0) {
    cancelGesture();
    return;
  }
  commitGesture(current ?? active, nextIndex);
}

function bindPointerListeners(
  node: HTMLElement,
  onMove: (event: PointerEvent) => void,
  onUp: (event: PointerEvent) => void,
  onLostCapture: () => void,
): () => void {
  node.addEventListener("pointermove", onMove);
  node.addEventListener("pointerup", onUp);
  node.addEventListener("pointercancel", onLostCapture);
  node.addEventListener("lostpointercapture", onLostCapture);
  return () => {
    node.removeEventListener("pointermove", onMove);
    node.removeEventListener("pointerup", onUp);
    node.removeEventListener("pointercancel", onLostCapture);
    node.removeEventListener("lostpointercapture", onLostCapture);
  };
}

type PointerControllerOptions = {
  surfaceRef: MutableRefObject<HTMLDivElement | null>;
  gestureRef: MutableRefObject<TreemapGesture | null>;
  gateRef: MutableRefObject<{ beginGesture: () => void }>;
  pointerApiRef: MutableRefObject<TreemapPointerApi>;
  beginTracked: BeginTracked;
  cancelGesture: CancelGesture;
  commitGesture: CommitGesture;
  setGesture: (gesture: TreemapGesture | null) => void;
  setAnnouncement: (message: string) => void;
};

type PointerStart = {
  event: ReactPointerEvent<Element>;
  item: TreemapMoveItem;
  surface: HTMLDivElement | null;
  gestureRef: MutableRefObject<TreemapGesture | null>;
  finishingPointerRef: MutableRefObject<boolean>;
  hoverItemRef: MutableRefObject<TreemapMoveItem | null>;
  lastPointRef: MutableRefObject<{ x: number; y: number }>;
  capturedPointerRef: MutableRefObject<{ node: HTMLElement; pointerId: number } | null>;
  pointerCleanupRef: MutableRefObject<(() => void) | null>;
  gateRef: MutableRefObject<{ beginGesture: () => void }>;
  beginTracked: BeginTracked;
  cancelGesture: CancelGesture;
  commitGesture: CommitGesture;
  detachPointer: () => void;
  releaseCapturedPointer: () => void;
  setGesture: (gesture: TreemapGesture | null) => void;
  setAnnouncement: (message: string) => void;
};

function holdSurfaceCapture(
  node: HTMLElement,
  pointerId: number,
  capturedPointerRef: PointerStart["capturedPointerRef"],
) {
  capturePointer(node, pointerId);
  capturedPointerRef.current = { node, pointerId };
  node.dataset.treemapPointerId = String(pointerId);
}

function startTreemapPointerGesture(start: PointerStart) {
  const { event, item } = start;
  if (event.button > 0) return;
  event.stopPropagation();
  if (start.gestureRef.current) start.cancelGesture(false);
  start.finishingPointerRef.current = false;
  start.hoverItemRef.current = null;
  start.lastPointRef.current = { x: event.clientX, y: event.clientY };
  const capturedNode = start.surface;
  if (!(capturedNode instanceof HTMLElement)) return;
  holdSurfaceCapture(capturedNode, event.pointerId, start.capturedPointerRef);
  const started = start.beginTracked(item, "pointer", { x: event.clientX, y: event.clientY });
  if (!started) return;

  const trackFromPoint = (clientX: number, clientY: number) => {
    start.lastPointRef.current = { x: clientX, y: clientY };
    const hovered = dropTargetFromPoint(clientX, clientY, item.id);
    if (!hovered) return null;
    start.hoverItemRef.current = hovered;
    const next = applyPointerHover(hovered, start.gestureRef.current);
    if (next) start.gestureRef.current = next;
    return hovered;
  };
  const finishPointer = () => {
    start.finishingPointerRef.current = true;
    start.detachPointer();
    start.releaseCapturedPointer();
  };
  const onMove = (moveEvent: PointerEvent) => {
    const active = start.gestureRef.current;
    if (!active || active.source !== "pointer" || start.finishingPointerRef.current) return;
    trackFromPoint(moveEvent.clientX, moveEvent.clientY);
    if (!shouldActivatePointer(active, moveEvent.clientX, moveEvent.clientY)) return;
    start.gateRef.current.beginGesture();
    const next = { ...(start.gestureRef.current ?? active), activated: true };
    start.gestureRef.current = next;
    start.setGesture(next);
    start.setAnnouncement(`Picked up ${next.kind} ${next.id}`);
  };
  const onUp = (upEvent: PointerEvent) => {
    if (start.finishingPointerRef.current) return;
    const active = start.gestureRef.current;
    const target = trackFromPoint(upEvent.clientX, upEvent.clientY) ?? start.hoverItemRef.current;
    finishPointer();
    resolveTreemapPointerUp(
      active,
      target,
      start.gestureRef.current,
      start.cancelGesture,
      start.commitGesture,
      () => {
        start.setGesture(null);
        start.gestureRef.current = null;
      },
    );
  };
  const onLostCapture = () => {
    if (!shouldCancelLostPointerCapture(start.finishingPointerRef.current, start.gestureRef.current)) {
      return;
    }
    start.cancelGesture();
  };
  start.pointerCleanupRef.current = bindPointerListeners(capturedNode, onMove, onUp, onLostCapture);
}

export function useTreemapPointerController(options: PointerControllerOptions) {
  const pointerCleanupRef = useRef<(() => void) | null>(null);
  const finishingPointerRef = useRef(false);
  const hoverItemRef = useRef<TreemapMoveItem | null>(null);
  const lastPointRef = useRef({ x: 0, y: 0 });
  const capturedPointerRef = useRef<{ node: HTMLElement; pointerId: number } | null>(null);
  const detachPointer = useCallback(() => {
    pointerCleanupRef.current?.();
    pointerCleanupRef.current = null;
  }, []);
  const releaseCapturedPointer = useCallback(() => {
    const held = capturedPointerRef.current;
    capturedPointerRef.current = null;
    if (held?.node instanceof HTMLElement) delete held.node.dataset.treemapPointerId;
    if (held) releasePointer(held.node, held.pointerId);
  }, []);
  const closeCapture = useCallback(() => {
    finishingPointerRef.current = true;
    detachPointer();
    releaseCapturedPointer();
  }, [detachPointer, releaseCapturedPointer]);
  options.pointerApiRef.current = { closeCapture, detachPointer };
  const onMovePointerDown = useCallback((
    event: ReactPointerEvent<Element>,
    item: TreemapMoveItem,
  ) => {
    startTreemapPointerGesture({
      event,
      item,
      surface: options.surfaceRef.current,
      gestureRef: options.gestureRef,
      finishingPointerRef,
      hoverItemRef,
      lastPointRef,
      capturedPointerRef,
      pointerCleanupRef,
      gateRef: options.gateRef,
      beginTracked: options.beginTracked,
      cancelGesture: options.cancelGesture,
      commitGesture: options.commitGesture,
      detachPointer,
      releaseCapturedPointer,
      setGesture: options.setGesture,
      setAnnouncement: options.setAnnouncement,
    });
  }, [
    detachPointer,
    options.beginTracked,
    options.cancelGesture,
    options.commitGesture,
    options.gateRef,
    options.gestureRef,
    options.setAnnouncement,
    options.setGesture,
    options.surfaceRef,
    releaseCapturedPointer,
  ]);
  return { onMovePointerDown, detachPointer, releaseCapturedPointer };
}
