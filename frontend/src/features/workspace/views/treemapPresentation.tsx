import {
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";
import { ResponsiveContainer, Treemap } from "recharts";
import type { TreemapMoveItem } from "./treemapMove";
import { isNavigatorGroup, type TreemapOrderNode } from "./treemapOrdering";
import {
  TREEMAP_MOVE_HANDLE_CLASS,
  rootScope,
  treemapMoveHandleLabel,
  type TreemapMode,
} from "./treemapViewState";
import type { TreemapGroup } from "./types";
import { STATUS_LABELS } from "./viewModels";

export const PRIORITY_COLORS: Record<string, string> = {
  P0: "var(--wsv-priority-p0-surface)",
  P1: "var(--wsv-priority-p1-surface)",
  P2: "var(--wsv-priority-p2-surface)",
  P3: "var(--wsv-priority-p3-surface)",
};

const GROUP_COLORS = [
  "var(--wsv-treemap-group-1)",
  "var(--wsv-treemap-group-2)",
  "var(--wsv-treemap-group-3)",
  "var(--wsv-treemap-group-4)",
  "var(--wsv-treemap-group-5)",
];

export type TreemapMovePointerHandler = (
  event: ReactPointerEvent<Element>,
  item: TreemapMoveItem,
) => void;

export type TreemapMoveKeyHandler = (
  event: ReactKeyboardEvent<Element>,
  item: TreemapMoveItem,
) => void;

export type TreemapCellGeometry = "hidden" | "tiny" | "compact" | "full";

export function treemapCellGeometry(width: number, height: number): TreemapCellGeometry {
  if (width < 2 || height < 2) return "hidden";
  if (width < 62 || height < 36) return "tiny";
  if (width < 105 || height < 62) return "compact";
  return "full";
}

export function isTreemapTaskNode(
  nodeKind: string | undefined,
  depth: number,
  taskId?: string,
): boolean {
  return nodeKind ? nodeKind === "task" : depth === 2 && Boolean(taskId);
}

export function shouldRenderTreemapGroup(
  nodeKind: string | undefined,
  depth: number,
  taskId?: string,
): boolean {
  return !isTreemapTaskNode(nodeKind, depth, taskId)
    && (nodeKind ? nodeKind !== "task" : depth === 1);
}

function clipText(value: string, width: number, characterWidth: number) {
  const count = Math.max(0, Math.floor(width / characterWidth));
  if (value.length <= count) return value;
  return count > 1 ? `${value.slice(0, count - 1)}…` : "";
}

function colorIdentity(objectiveId: string | null) {
  return objectiveId ?? "\u0000operations";
}

function treemapPriorityFill(priority: string): string {
  return PRIORITY_COLORS[priority] || PRIORITY_COLORS.P2;
}

function treemapTaskStroke(selected: boolean) {
  return {
    stroke: selected
      ? "var(--wsv-treemap-selection)"
      : "var(--wsv-treemap-cell-border)",
    strokeWidth: selected ? 2.5 : 0.7,
    strokeOpacity: selected ? 1 : 0.38,
  };
}

function treemapTaskAriaLabel(
  taskId: string,
  name: string,
  objectiveIds: readonly string[],
): string {
  return `${taskId}: ${name}${objectiveIds.length > 1 ? `; aligned to ${objectiveIds.join(", ")}` : ""}`;
}

function treemapTaskTitle(name: string, objectiveIds: readonly string[]): string {
  return objectiveIds.length > 1 ? `${name} · ${objectiveIds.join(" + ")}` : name;
}

function treemapTaskMeta(
  taskId: string,
  priority: string,
  status: string,
  objectiveIds: readonly string[],
): string {
  const statusLabel = STATUS_LABELS[status as keyof typeof STATUS_LABELS] || status;
  const alignmentLabel = objectiveIds.length > 1 ? ` · ${objectiveIds.join(" + ")}` : "";
  return `${taskId} · ${priority} · ${statusLabel}${alignmentLabel}`;
}

function itemDataset(item: TreemapMoveItem) {
  return {
    "data-treemap-item": "true",
    "data-treemap-scope": item.scope,
    "data-treemap-id": item.id,
    "data-treemap-kind": item.kind,
  } as const;
}

interface TreemapContentProps {
  nodeKind?: string;
  depth?: number;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  name?: string;
  taskId?: string;
  priority?: string;
  status?: string;
  objectiveId?: string;
  objectiveIds?: readonly string[];
  moveScope?: string;
  selectedTaskId?: string | null;
  movePressedId?: string | null;
  onSelectTask: (taskId: string) => void;
  onMovePointerDown?: TreemapMovePointerHandler;
  onMoveKeyDown?: TreemapMoveKeyHandler;
}

interface TreemapObjectiveGroupProps {
  x: number;
  y: number;
  width: number;
  height: number;
  name: string;
  objectiveId: string | null;
}

export function TreemapObjectiveGroup({
  x,
  y,
  width,
  height,
  name,
  objectiveId,
}: TreemapObjectiveGroupProps) {
  const colorIndex = Math.abs(
    [...colorIdentity(objectiveId)].reduce((value, character) => value + character.charCodeAt(0), 0),
  );
  return (
    <g>
      <rect
        x={x + 1}
        y={y + 1}
        width={Math.max(0, width - 2)}
        height={Math.max(0, height - 2)}
        rx={10}
        fill={GROUP_COLORS[colorIndex % GROUP_COLORS.length]}
        fillOpacity={0.32}
        stroke="var(--wsv-treemap-group-border)"
        strokeOpacity={0.5}
      />
      {width > 90 && height > 28 ? (
        <text x={x + 10} y={y + 18} className="wsv-treemap-group-label">
          {clipText(name, width - 18, 7.1)}
        </text>
      ) : null}
    </g>
  );
}

function TreemapMoveHandle({
  item,
  pressed,
  html,
  x = 0,
  y = 0,
  onPointerDown,
  onKeyDown,
}: {
  item: TreemapMoveItem;
  pressed: boolean;
  html: boolean;
  x?: number;
  y?: number;
  onPointerDown: TreemapMovePointerHandler;
  onKeyDown: TreemapMoveKeyHandler;
}) {
  const label = treemapMoveHandleLabel(item.kind, item.id);
  const attrs = {
    className: TREEMAP_MOVE_HANDLE_CLASS,
    "aria-label": label,
    "aria-pressed": pressed || undefined,
    ...itemDataset(item),
  } as const;
  if (html) {
    return (
      <button
        type="button"
        {...attrs}
        onClick={(event) => event.stopPropagation()}
        onPointerDown={(event) => onPointerDown(event, item)}
        onKeyDown={(event) => onKeyDown(event, item)}
      >
        <span aria-hidden="true">↕</span>
      </button>
    );
  }
  return (
    <foreignObject x={x} y={y} width={22} height={22}>
      <button
        type="button"
        {...attrs}
        onClick={(event) => event.stopPropagation()}
        onPointerDown={(event) => onPointerDown(event, item)}
        onKeyDown={(event) => onKeyDown(event, item)}
      >
        <span aria-hidden="true">↕</span>
      </button>
    </foreignObject>
  );
}

export function TreemapObjectiveNavigator({
  groups,
  mode = "legacy",
  movePressedId = null,
  onSelectObjective,
  onMovePointerDown,
  onMoveKeyDown,
}: {
  groups: readonly TreemapOrderNode[];
  mode?: TreemapMode;
  movePressedId?: string | null;
  onSelectObjective: (objectiveId: string) => void;
  onMovePointerDown?: TreemapMovePointerHandler;
  onMoveKeyDown?: TreemapMoveKeyHandler;
}) {
  const alignedGroups = groups.filter((group) => isNavigatorGroup(group, mode));
  if (!alignedGroups.length) return null;
  const scope = rootScope(mode);
  return (
    <nav aria-label="Treemap objective navigation" className="wsv-treemap-objectives">
      {alignedGroups.map((group) => {
        const objectiveId = group.objectiveId as string;
        const item = { kind: "objective" as const, id: objectiveId, scope };
        return (
          <span
            className="wsv-treemap-objective-item"
            {...itemDataset(item)}
            key={group.groupKey ?? objectiveId}
          >
            {onMovePointerDown && onMoveKeyDown ? (
              <TreemapMoveHandle
                html
                item={item}
                pressed={movePressedId === objectiveId}
                onPointerDown={onMovePointerDown}
                onKeyDown={onMoveKeyDown}
              />
            ) : null}
            <button
              aria-label={`Focus objective ${objectiveId}`}
              onClick={() => onSelectObjective(objectiveId)}
              title={group.name}
              type="button"
            >
              {objectiveId}
            </button>
          </span>
        );
      })}
    </nav>
  );
}

function TreemapTaskLabels({
  x,
  y,
  width,
  name,
  meta,
  geometry,
}: {
  x: number;
  y: number;
  width: number;
  name: string;
  meta: string;
  geometry: TreemapCellGeometry;
}) {
  if (geometry === "tiny" || geometry === "hidden") return null;
  return (
    <>
      <text x={x + 10} y={y + 20} className="wsv-treemap-task-title">
        {clipText(name, width - 18, 6.5)}
      </text>
      {geometry === "full" ? (
        <text x={x + 10} y={y + 38} className="wsv-treemap-task-meta">
          {clipText(meta, width - 18, 5.8)}
        </text>
      ) : null}
    </>
  );
}

function selectTaskFromKeyboard(
  event: ReactKeyboardEvent<Element>,
  taskId: string,
  onSelectTask: (taskId: string) => void,
) {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  onSelectTask(taskId);
}

type TreemapTaskCellProps = {
  x: number;
  y: number;
  width: number;
  height: number;
  name: string;
  taskId: string;
  priority: string;
  status: string;
  objectiveIds: readonly string[];
  moveScope?: string;
  selectedTaskId?: string | null;
  movePressedId?: string | null;
  onSelectTask: (taskId: string) => void;
  onMovePointerDown?: TreemapMovePointerHandler;
  onMoveKeyDown?: TreemapMoveKeyHandler;
};

function TreemapTaskCell({
  x,
  y,
  width,
  height,
  name,
  taskId,
  priority,
  status,
  objectiveIds,
  moveScope,
  selectedTaskId,
  movePressedId,
  onSelectTask,
  onMovePointerDown,
  onMoveKeyDown,
}: TreemapTaskCellProps) {
  const selected = taskId === selectedTaskId;
  const geometry = treemapCellGeometry(width, height);
  const item = moveScope
    ? { kind: "task" as const, id: taskId, scope: moveScope }
    : null;
  const stroke = treemapTaskStroke(selected);
  return (
    <g className={`wsv-treemap-cell ${selected ? "is-selected" : ""}`}>
      <g
        role="button"
        tabIndex={0}
        aria-label={treemapTaskAriaLabel(taskId, name, objectiveIds)}
        {...(item ? itemDataset(item) : {})}
        onClick={() => onSelectTask(taskId)}
        onKeyDown={(event) => selectTaskFromKeyboard(event, taskId, onSelectTask)}
      >
        <title>{treemapTaskTitle(name, objectiveIds)}</title>
        <rect
          x={x + 2}
          y={y + 2}
          width={Math.max(0, width - 4)}
          height={Math.max(0, height - 4)}
          rx={7}
          fill={treemapPriorityFill(priority)}
          stroke={stroke.stroke}
          strokeWidth={stroke.strokeWidth}
          strokeOpacity={stroke.strokeOpacity}
        />
        <TreemapTaskLabels
          geometry={geometry}
          meta={treemapTaskMeta(taskId, priority, status, objectiveIds)}
          name={name}
          width={width}
          x={x}
          y={y}
        />
      </g>
      {item && onMovePointerDown && onMoveKeyDown ? (
        <TreemapMoveHandle
          html={false}
          item={item}
          pressed={movePressedId === taskId}
          x={x + Math.max(6, width - 26)}
          y={y + 6}
          onPointerDown={onMovePointerDown}
          onKeyDown={onMoveKeyDown}
        />
      ) : null}
    </g>
  );
}

function TreemapContent({
  nodeKind,
  depth = 0,
  x = 0,
  y = 0,
  width = 0,
  height = 0,
  name = "",
  taskId,
  priority = "P2",
  status = "open",
  objectiveId = "",
  objectiveIds = [],
  moveScope,
  selectedTaskId,
  movePressedId,
  onSelectTask,
  onMovePointerDown,
  onMoveKeyDown,
}: TreemapContentProps) {
  if (treemapCellGeometry(width, height) === "hidden") return null;
  if (shouldRenderTreemapGroup(nodeKind, depth, taskId)) {
    return (
      <TreemapObjectiveGroup
        height={height}
        name={name}
        objectiveId={objectiveId}
        width={width}
        x={x}
        y={y}
      />
    );
  }
  if (!isTreemapTaskNode(nodeKind, depth, taskId) || !taskId) return null;
  return (
    <TreemapTaskCell
      height={height}
      movePressedId={movePressedId}
      moveScope={moveScope}
      name={name}
      objectiveIds={objectiveIds}
      onMoveKeyDown={onMoveKeyDown}
      onMovePointerDown={onMovePointerDown}
      onSelectTask={onSelectTask}
      priority={priority}
      selectedTaskId={selectedTaskId}
      status={status}
      taskId={taskId}
      width={width}
      x={x}
      y={y}
    />
  );
}

export function TreemapEmpty() {
  return (
    <div className="wsv-empty" role="status">
      <strong>No work matches these filters</strong>
      <span>Clear a filter to restore the objective map.</span>
    </div>
  );
}

export function TreemapSurface({
  surfaceRef,
  announcement,
  orderedGroups,
  selectedTaskId,
  movePressedId,
  mode,
  onSelectTask,
  onSelectObjective,
  onMovePointerDown,
  onMoveKeyDown,
  onResetOrder,
}: {
  surfaceRef: RefObject<HTMLDivElement | null>;
  announcement: string;
  orderedGroups: readonly TreemapOrderNode[];
  selectedTaskId?: string | null;
  movePressedId: string | null;
  mode: TreemapMode;
  onSelectTask: (taskId: string) => void;
  onSelectObjective: (objectiveId: string) => void;
  onMovePointerDown: TreemapMovePointerHandler;
  onMoveKeyDown: TreemapMoveKeyHandler;
  onResetOrder: () => void;
}) {
  return (
    <div
      ref={surfaceRef}
      className="wsv-treemap"
      data-treemap-surface="true"
      aria-label="Tasks grouped by objective"
    >
      <div className="wsv-treemap-live" aria-live="polite" aria-atomic="true">{announcement}</div>
      <ResponsiveContainer width="100%" height="100%">
        <Treemap
          data={orderedGroups as unknown as TreemapGroup[]}
          dataKey="size"
          nameKey="name"
          aspectRatio={4 / 3}
          isAnimationActive={false}
          content={
            <TreemapContent
              selectedTaskId={selectedTaskId}
              movePressedId={movePressedId}
              onSelectTask={onSelectTask}
              onMovePointerDown={onMovePointerDown}
              onMoveKeyDown={onMoveKeyDown}
            />
          }
        />
      </ResponsiveContainer>
      <TreemapObjectiveNavigator
        groups={orderedGroups}
        mode={mode}
        movePressedId={movePressedId}
        onSelectObjective={onSelectObjective}
        onMovePointerDown={onMovePointerDown}
        onMoveKeyDown={onMoveKeyDown}
      />
      <button type="button" className="wsv-treemap-reset" onClick={onResetOrder}>
        Reset treemap order
      </button>
      <div className="wsv-treemap-legend" aria-label="Priority legend">
        {Object.entries(PRIORITY_COLORS).map(([priority, color]) => (
          <span key={priority}>
            <i style={{ background: color }} /> {priority}
          </span>
        ))}
      </div>
    </div>
  );
}
