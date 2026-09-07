import { type PointerEvent as ReactPointerEvent } from "react";
import {
  KeyboardCode,
  PointerSensor,
  type Announcements,
  type CollisionDetection,
  type KeyboardCoordinateGetter,
  type PointerSensorOptions,
  type UniqueIdentifier,
} from "@dnd-kit/core";

import { TASK_STATUSES, type TaskStatus, type WorkspaceTask } from "./types";
import { STATUS_LABELS, asTaskStatus } from "./viewModels";

const NESTED_CONTROL_SELECTOR =
  "a, button, input, textarea, select, option, label, [contenteditable]:not([contenteditable='false'])";
const SELECTABLE_TEXT_SELECTOR = "h3, p";

export const BOARD_DRAG_INSTRUCTIONS = {
  draggable:
    "To pick up a card, press Space or Enter on its move handle. While dragging, use the arrow keys to move it between status columns. Press Space or Enter to drop it, or Escape to cancel.",
};

function eventElement(target: EventTarget | null): Element | null {
  if (target instanceof Element) return target;
  if (target instanceof CharacterData) return target.parentElement;
  return null;
}

export function isBoardCardPointerBlocked(target: EventTarget | null): boolean {
  const element = eventElement(target);
  if (!element) return false;
  if (element.closest(".wsv-drag-handle")) return false;
  if (element.closest(SELECTABLE_TEXT_SELECTOR)) return true;
  return Boolean(element.closest(NESTED_CONTROL_SELECTOR));
}

export class BoardCardPointerSensor extends PointerSensor {
  static activators = [
    {
      eventName: "onPointerDown" as const,
      handler: (
        { nativeEvent: event }: ReactPointerEvent,
        { onActivation }: PointerSensorOptions,
      ) => {
        if (!event.isPrimary || event.button !== 0) return false;
        if (isBoardCardPointerBlocked(event.target)) return false;
        onActivation?.({ event });
        return true;
      },
    },
  ];
}

function orderedStatusColumns(
  droppableRects: { get(id: UniqueIdentifier): { left: number; width: number } | undefined },
) {
  return TASK_STATUSES.flatMap((status) => {
    const rect = droppableRects.get(status);
    return rect ? [{ status, rect }] : [];
  }).sort((left, right) => left.rect.left - right.rect.left);
}

function columnIndexAt(
  columns: readonly { status: TaskStatus; rect: { left: number; width: number } }[],
  x: number,
) {
  const containing = columns.findIndex(
    (column) => x >= column.rect.left && x < column.rect.left + column.rect.width,
  );
  if (containing >= 0) return containing;
  let best = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  columns.forEach((column, index) => {
    const center = column.rect.left + column.rect.width / 2;
    const distance = Math.abs(center - x);
    if (distance < bestDistance) {
      best = index;
      bestDistance = distance;
    }
  });
  return best;
}

function statusIntentX(
  pointerCoordinates: { x: number } | null,
  collisionRect: { left: number; width: number },
) {
  return pointerCoordinates?.x ?? collisionRect.left + collisionRect.width / 2;
}

export const boardStatusCollision: CollisionDetection = ({
  collisionRect,
  droppableRects,
  droppableContainers,
  pointerCoordinates,
}) => {
  const columns = orderedStatusColumns(droppableRects);
  if (!columns.length) return [];
  const match = columns[columnIndexAt(columns, statusIntentX(pointerCoordinates, collisionRect))];
  if (!match) return [];
  const container = droppableContainers.find((entry) => String(entry.id) === match.status);
  if (!container) return [];
  return [{ id: container.id, data: { droppableContainer: container, value: 0 } }];
};

export const boardKeyboardCoordinates: KeyboardCoordinateGetter = (
  event,
  { currentCoordinates, context },
) => {
  if (event.code === KeyboardCode.Down || event.code === KeyboardCode.Up) {
    const step = context.collisionRect?.height || 48;
    return {
      x: currentCoordinates.x,
      y: currentCoordinates.y + (event.code === KeyboardCode.Down ? step : -step),
    };
  }
  if (event.code !== KeyboardCode.Right && event.code !== KeyboardCode.Left) {
    return undefined;
  }
  const columns = orderedStatusColumns(context.droppableRects);
  if (!columns.length) return undefined;
  const cardWidth = context.collisionRect?.width ?? 0;
  const index = columnIndexAt(columns, currentCoordinates.x + cardWidth / 2);
  const next = columns[index + (event.code === KeyboardCode.Right ? 1 : -1)] ?? columns[index];
  return {
    x: next.rect.left + next.rect.width / 2 - cardWidth / 2,
    y: currentCoordinates.y,
  };
};

function taskDragLabel(task: WorkspaceTask | undefined, id: UniqueIdentifier): string {
  return task ? `${task.id}: ${task.title}` : String(id);
}

function columnStatusLabel(overId: UniqueIdentifier | undefined | null): string | null {
  const status = String(overId ?? "");
  return TASK_STATUSES.includes(status as TaskStatus)
    ? STATUS_LABELS[status as TaskStatus]
    : null;
}

export function createBoardAnnouncements(
  taskById: ReadonlyMap<string, WorkspaceTask>,
): Announcements {
  return {
    onDragStart({ active }) {
      const task = taskById.get(String(active.id));
      const current = task ? STATUS_LABELS[asTaskStatus(task.status)] : "its column";
      return `Picked up ${taskDragLabel(task, active.id)}. Current status ${current}.`;
    },
    onDragOver({ active, over }) {
      const column = over ? columnStatusLabel(over.id) : null;
      const label = taskDragLabel(taskById.get(String(active.id)), active.id);
      if (!column) return `${label} is not over a status column.`;
      return `${label} is over ${column}.`;
    },
    onDragEnd({ active, over }) {
      const column = over ? columnStatusLabel(over.id) : null;
      const label = taskDragLabel(taskById.get(String(active.id)), active.id);
      if (!column) return `Move cancelled. ${label} returned to its column.`;
      return `Moved ${label} to ${column}.`;
    },
    onDragCancel({ active }) {
      const label = taskDragLabel(taskById.get(String(active.id)), active.id);
      return `Move cancelled. ${label} returned to its column.`;
    },
  };
}
