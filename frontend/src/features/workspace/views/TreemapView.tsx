import { useMemo } from "react";
import { ResponsiveContainer, Treemap } from "recharts";

import type {
  TreemapGroup,
  WorkspaceObjective,
  WorkspaceTask,
} from "./types";
import { STATUS_LABELS, buildTreemapGroups } from "./viewModels";
import { buildOutcomeTreemap } from "./keyResultViewModel";
import type { KeyResultProjection } from "./keyResultModel";

interface TreemapViewProps {
  tasks: readonly WorkspaceTask[];
  objectives: readonly WorkspaceObjective[];
  /** When supplied, the map groups by outcome instead of Objective alignment. */
  keyResultProjection?: KeyResultProjection;
  selectedTaskId?: string | null;
  onSelectTask: (taskId: string) => void;
  onSelectObjective: (objectiveId: string) => void;
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

/**
 * GN5: colour identity is a presentation concern. A null Objective is the
 * Operations bucket and gets a stable colour of its own; no reserved raw
 * sentinel is written back into the data, and legal IDs keep their colour.
 */
function colorIdentity(objectiveId: string | null) {
  return objectiveId ?? "\u0000operations";
}

interface TreemapObjectiveGroupProps {
  x: number;
  y: number;
  width: number;
  height: number;
  name: string;
  // GN5: the Operations bucket has no Objective. The typed null reaches the
  // visual layer and is given its own colour identity there; the data keeps it.
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

export function TreemapObjectiveNavigator({
  groups,
  onSelectObjective,
}: {
  groups: readonly TreemapGroup[];
  onSelectObjective: (objectiveId: string) => void;
}) {
  // NG-R1: a real Objective may legitimately be called "none" or "multiple".
  // Navigability follows the typed group identity: a top-level Objective node
  // with an actual Objective ID. The legacy two-level fallback has no nodeKind,
  // so it keeps excluding its own synthetic buckets by their reserved IDs.
  const alignedGroups = groups.filter((group) => {
    const nodeKind = (group as { nodeKind?: string }).nodeKind;
    if (nodeKind) {
      return nodeKind === "objective" && typeof group.objectiveId === "string";
    }
    return group.objectiveId !== "none" && group.objectiveId !== "multiple";
  });
  if (!alignedGroups.length) return null;
  return (
    <nav aria-label="Treemap objective navigation" className="wsv-treemap-objectives">
      {alignedGroups.map(({ objectiveId, name, ...group }) => (
        <button
          aria-label={`Focus objective ${objectiveId}`}
          key={(group as { groupKey?: string }).groupKey ?? objectiveId}
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
  selectedTaskId,
  onSelectTask,
}: TreemapContentProps) {
  if (width < 2 || height < 2) return null;

  // Typed hierarchies identify their leaves by kind; the legacy two-level
  // fallback still has its Tasks at depth 2 with no kind supplied.
  const isTask = nodeKind ? nodeKind === "task" : depth === 2 && Boolean(taskId);
  if (!isTask && (nodeKind ? nodeKind !== "task" : depth === 1)) {
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

  if (!isTask || !taskId) return null;
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
  keyResultProjection,
  selectedTaskId,
  onSelectTask,
  onSelectObjective,
}: TreemapViewProps) {
  const groups = useMemo(
    () => {
      // The legacy grouping is also the source of the existing size formula, so
      // outcome grouping reuses each Task's already-computed area verbatim.
      const legacy = buildTreemapGroups(tasks, objectives);
      if (!keyResultProjection) return legacy;
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
        // NG-R1: the real nullable identity survives to the navigator. A null
        // parent is the Operations bucket, never the literal string "none".
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
    },
    [tasks, objectives, keyResultProjection],
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
          // Recharts consumes a plain tree; views/types.ts is not owned here,
          // so the typed hierarchy crosses that boundary with one narrow cast.
          data={groups as unknown as TreemapGroup[]}
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
      <TreemapObjectiveNavigator groups={groups as unknown as TreemapGroup[]} onSelectObjective={onSelectObjective} />
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
