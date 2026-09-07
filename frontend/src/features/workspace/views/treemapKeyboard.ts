import {
  useCallback,
  useEffect,
  type KeyboardEvent as ReactKeyboardEvent,
  type MutableRefObject,
} from "react";

import { formatTreemapPosition, keyboardTargetIndex } from "./treemapOrdering";
import { kindTitle, type TreemapGesture, type TreemapMoveItem } from "./treemapMove";
import { treemapMoveHandleLabel } from "./treemapViewState";

export type TreemapMoveKeyCommand =
  | { action: "none" }
  | { action: "consume" }
  | { action: "pickup" }
  | { action: "commit" }
  | { action: "cancel"; consume: boolean }
  | { action: "preview"; index: number };

type GestureActions = {
  gestureRef: MutableRefObject<TreemapGesture | null>;
  beginTracked: (
    item: TreemapMoveItem,
    source: TreemapGesture["source"],
    point: { x: number; y: number },
  ) => TreemapGesture | null;
  cancelGesture: (announce?: boolean, message?: string) => void;
  commitGesture: (active: TreemapGesture, nextIndex: number) => void;
  setGesture: (gesture: TreemapGesture | null) => void;
  setAnnouncement: (message: string) => void;
};

function isPickupKey(key: string): boolean {
  return key === "Enter" || key === " ";
}

function isMatchingKeyboardMove(active: TreemapGesture | null, item: TreemapMoveItem): boolean {
  return Boolean(active && active.source === "keyboard" && active.id === item.id);
}

function decodePickupKey(
  active: TreemapGesture | null,
  item: TreemapMoveItem,
): TreemapMoveKeyCommand {
  if (active && active.source === "pointer") return { action: "consume" };
  if (!active || active.id !== item.id || active.scope !== item.scope) return { action: "pickup" };
  return { action: "commit" };
}

function decodeEscapeWhenIdle(
  key: string,
  active: TreemapGesture | null,
): TreemapMoveKeyCommand {
  if (key === "Escape" && active) return { action: "cancel", consume: true };
  return { action: "none" };
}

export function decodeTreemapMoveKey(
  key: string,
  item: TreemapMoveItem,
  active: TreemapGesture | null,
): TreemapMoveKeyCommand {
  if (isPickupKey(key)) return decodePickupKey(active, item);
  if (!isMatchingKeyboardMove(active, item) || !active) return decodeEscapeWhenIdle(key, active);
  if (key === "Escape") return { action: "cancel", consume: true };
  const index = keyboardTargetIndex(active.currentIndex, active.visibleIds.length, key);
  if (index === null) return { action: "none" };
  return { action: "preview", index };
}

function runPickup(
  event: ReactKeyboardEvent<Element>,
  item: TreemapMoveItem,
  actions: GestureActions,
) {
  event.preventDefault();
  event.stopPropagation();
  if (actions.gestureRef.current) actions.cancelGesture(false);
  actions.beginTracked(item, "keyboard", { x: 0, y: 0 });
}

function runCommit(event: ReactKeyboardEvent<Element>, actions: GestureActions) {
  event.preventDefault();
  event.stopPropagation();
  const active = actions.gestureRef.current;
  if (!active) return;
  actions.commitGesture(active, active.currentIndex);
}

function runCancel(
  event: ReactKeyboardEvent<Element>,
  command: Extract<TreemapMoveKeyCommand, { action: "cancel" }>,
  actions: GestureActions,
) {
  if (command.consume) event.preventDefault();
  actions.cancelGesture();
}

function runPreview(
  event: ReactKeyboardEvent<Element>,
  index: number,
  actions: GestureActions,
) {
  event.preventDefault();
  const active = actions.gestureRef.current;
  if (!active) return;
  const next = { ...active, currentIndex: index };
  actions.gestureRef.current = next;
  actions.setGesture(next);
  actions.setAnnouncement(
    `${kindTitle(active.kind)} ${active.id} at position ${formatTreemapPosition(index, active.visibleIds.length)}`,
  );
}

export function runTreemapMoveKeyCommand(
  event: ReactKeyboardEvent<Element>,
  item: TreemapMoveItem,
  command: TreemapMoveKeyCommand,
  actions: GestureActions,
) {
  if (command.action === "none") return;
  if (command.action === "consume") {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  if (command.action === "pickup") {
    runPickup(event, item, actions);
    return;
  }
  if (command.action === "commit") {
    runCommit(event, actions);
    return;
  }
  if (command.action === "cancel") {
    runCancel(event, command, actions);
    return;
  }
  runPreview(event, command.index, actions);
}

export function useTreemapKeyboardMove(
  gesture: TreemapGesture | null,
  actions: GestureActions,
) {
  const { gestureRef, beginTracked, cancelGesture, commitGesture, setGesture, setAnnouncement } = actions;
  const onMoveKeyDown = useCallback((
    event: ReactKeyboardEvent<Element>,
    item: TreemapMoveItem,
  ) => {
    const command = decodeTreemapMoveKey(event.key, item, gestureRef.current);
    runTreemapMoveKeyCommand(event, item, command, {
      gestureRef, beginTracked, cancelGesture, commitGesture, setGesture, setAnnouncement,
    });
  }, [beginTracked, cancelGesture, commitGesture, gestureRef, setAnnouncement, setGesture]);

  useEffect(() => {
    if (!gesture) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      cancelGesture();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [cancelGesture, gesture]);

  useEffect(() => {
    if (gesture?.source !== "keyboard") return;
    const label = treemapMoveHandleLabel(gesture.kind, gesture.id);
    const handle = document.querySelector(`[aria-label="${CSS.escape(label)}"]`);
    if (handle instanceof HTMLElement) handle.focus();
  }, [gesture]);

  return onMoveKeyDown;
}
