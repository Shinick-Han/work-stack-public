import { useMemo } from "react";
import { ResponsiveContainer, Treemap } from "recharts";

import type {
  TreemapGroup,
  WorkspaceObjective,
  WorkspaceTask,
} from "./types";
import { STATUS_LABELS, buildTreemapGroups } from "./viewModels";

interface TreemapViewProps {
  tasks: readonly WorkspaceTask[];
  objectives: readonly WorkspaceObjective[];
  selectedTaskId?: string | null;
  onSelectTask: (taskId: string) => void;
  onSelectObjective: (objectiveId: string) => void;
}

interface TreemapContentProps {
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
  selectedTaskId?: string | null;
  onSelectTask: (taskId: string) => void;
}

const PRIORITY_COLORS: Record<string, string> = {
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

function clipText(value: string, width: number, characterWidth: number) {
  const count = Math.max(0, Math.floor(width / characterWidth));
  if (value.length <= count) return value;
  return count > 1 ? `${value.slice(0, count - 1)}…` : "";
}

interface TreemapObjectiveGroupProps {
  x: number;
  y: number;
  width: number;
  height: number;
  name: string;
  objectiveId: string;
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
    [...objectiveId].reduce((value, character) => value + character.charCodeAt(0), 0),
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

export function TreemapObjectiveNavigator({
  groups,
  onSelectObjective,
}: {
  groups: readonly TreemapGroup[];
  onSelectObjective: (objectiveId: string) => void;
}) {
  const alignedGroups = groups.filter(({ objectiveId }) => objectiveId !== "none" && objectiveId !== "multiple");
  if (!alignedGroups.length) return null;
  return (
    <nav aria-label="Treemap objective navigation" className="wsv-treemap-objectives">
      {alignedGroups.map(({ objectiveId, name }) => (
        <button
          aria-label={`Focus objective ${objectiveId}`}
          key={objectiveId}
          onClick={() => onSelectObjective(objectiveId)}
          title={name}
          type="button"
        >
          {objectiveId}
        </button>
      ))}
    </nav>
  );
}

function TreemapContent({
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
  selectedTaskId,
  onSelectTask,
}: TreemapContentProps) {
  if (width < 2 || height < 2) return null;

  if (depth === 1) {
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

  if (depth !== 2 || !taskId) return null;
  const selected = taskId === selectedTaskId;
  const compact = width < 105 || height < 62;
  const tiny = width < 62 || height < 36;
  const statusLabel = STATUS_LABELS[status as keyof typeof STATUS_LABELS] || status;
  const alignmentLabel = objectiveIds.length > 1 ? ` · ${objectiveIds.join(" + ")}` : "";

  return (
    <g
      role="button"
      tabIndex={0}
      aria-label={`${taskId}: ${name}${objectiveIds.length > 1 ? `; aligned to ${objectiveIds.join(", ")}` : ""}`}
      className={`wsv-treemap-cell ${selected ? "is-selected" : ""}`}
      onClick={() => onSelectTask(taskId)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelectTask(taskId);
        }
      }}
    >
      <title>{objectiveIds.length > 1 ? `${name} · ${objectiveIds.join(" + ")}` : name}</title>
      <rect
        x={x + 2}
        y={y + 2}
        width={Math.max(0, width - 4)}
        height={Math.max(0, height - 4)}
        rx={7}
        fill={PRIORITY_COLORS[priority] || PRIORITY_COLORS.P2}
        stroke={selected
          ? "var(--wsv-treemap-selection)"
          : "var(--wsv-treemap-cell-border)"}
        strokeWidth={selected ? 2.5 : 0.7}
        strokeOpacity={selected ? 1 : 0.38}
      />
      {!tiny ? (
        <>
          <text x={x + 10} y={y + 20} className="wsv-treemap-task-title">
            {clipText(name, width - 18, 6.5)}
          </text>
          {!compact ? (
            <text x={x + 10} y={y + 38} className="wsv-treemap-task-meta">
              {clipText(`${taskId} · ${priority} · ${statusLabel}${alignmentLabel}`, width - 18, 5.8)}
            </text>
          ) : null}
        </>
      ) : null}
    </g>
  );
}

export function TreemapView({
  tasks,
  objectives,
  selectedTaskId,
  onSelectTask,
  onSelectObjective,
}: TreemapViewProps) {
  const groups = useMemo(
    () => buildTreemapGroups(tasks, objectives),
    [tasks, objectives],
  );

  if (!groups.length) {
    return (
      <div className="wsv-empty" role="status">
        <strong>No work matches these filters</strong>
        <span>Clear a filter to restore the objective map.</span>
      </div>
    );
  }

  return (
    <div className="wsv-treemap" aria-label="Tasks grouped by objective">
      <ResponsiveContainer width="100%" height="100%">
        <Treemap
          data={groups as TreemapGroup[]}
          dataKey="size"
          nameKey="name"
          aspectRatio={4 / 3}
          isAnimationActive={false}
          content={
            <TreemapContent
              selectedTaskId={selectedTaskId}
              onSelectTask={onSelectTask}
            />
          }
        />
      </ResponsiveContainer>
      <TreemapObjectiveNavigator groups={groups} onSelectObjective={onSelectObjective} />
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
