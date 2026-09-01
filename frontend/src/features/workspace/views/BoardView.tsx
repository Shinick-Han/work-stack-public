import { useEffect, useMemo, useRef, useState } from "react";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  closestCorners,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";

import {
  TASK_STATUSES,
  type TaskStatus,
  type WorkspaceTask,
} from "./types";
import {
  STATUS_LABELS,
  asTaskPriority,
  asTaskStatus,
  taskSubtaskProgress,
  tasksByStatus,
} from "./viewModels";
import {
  blockingDependenciesFromIndex,
  indexDependencyTasks,
  type TaskBlocker,
} from "../../../domain/taskRelationships";
import { useLocalToday } from "../../focus/useLocalToday";
import { TaskDueTiming } from "./TaskDueTiming";

interface BoardViewProps {
  tasks: readonly WorkspaceTask[];
  referenceTasks?: readonly WorkspaceTask[];
  selectedTaskId?: string | null;
  onSelectTask: (taskId: string) => void;
  onSelectObjective?: (objectiveId: string) => void;
  today?: string;
  onChangeTaskStatus: (
    taskId: string,
    status: TaskStatus,
  ) => Promise<void>;
}

interface TaskCardProps {
  task: WorkspaceTask;
  status: TaskStatus;
  selected: boolean;
  pending: boolean;
  overlay?: boolean;
  blockers: readonly TaskBlocker[];
  today: string;
  onSelect: () => void;
  onSelectTask: (taskId: string) => void;
  onSelectObjective?: (objectiveId: string) => void;
  onChangeStatus: (status: TaskStatus) => void;
}

function TaskCard({
  task,
  status,
  selected,
  pending,
  overlay = false,
  blockers,
  today,
  onSelect,
  onSelectTask,
  onSelectObjective,
  onChangeStatus,
}: TaskCardProps) {
  const draggable = useDraggable({
    id: overlay ? `overlay:${task.id}` : task.id,
    disabled: pending || overlay,
  });
  const transform = draggable.transform
    ? `translate3d(${draggable.transform.x}px, ${draggable.transform.y}px, 0)`
    : undefined;
  const priority = asTaskPriority(task.priority);
  const subtaskProgress = taskSubtaskProgress(task);

  return (
    <article
      ref={overlay ? undefined : draggable.setNodeRef}
      className={[
        "wsv-task-card",
        selected ? "is-selected" : "",
        draggable.isDragging ? "is-dragging" : "",
        overlay ? "is-overlay" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      data-priority={priority}
      style={{ transform }}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect();
        }
      }}
      tabIndex={0}
      aria-label={`${task.id}: ${task.title}`}
    >
      <header>
        <span className={`wsv-priority wsv-priority--${priority}`}>
          {priority}
        </span>
        <button
          className="wsv-drag-handle"
          type="button"
          aria-label={`Move ${task.title}`}
          title="Drag to another status"
          disabled={pending || overlay}
          onClick={(event) => event.stopPropagation()}
          {...(overlay ? {} : draggable.attributes)}
          {...(overlay ? {} : draggable.listeners)}
        >
          ⠿
        </button>
      </header>
      <h3>{task.title}</h3>
      <p>{task.detail || "No detail yet."}</p>
      {blockers.length ? (
        <div className="wsv-readiness wsv-readiness--blocked" aria-label={`${task.id} readiness`}>
          <span>Blocked by</span>
          {blockers.map((blocker) => (
            <button
              key={blocker.id}
              type="button"
              aria-label={`Blocked by ${blocker.id}${blocker.title ? `: ${blocker.title}` : ""}`}
              title={blocker.title ?? "Dependency is missing from the current workspace"}
              disabled={overlay || !blocker.title}
              onClick={(event) => {
                event.stopPropagation();
                onSelectTask(blocker.id);
              }}
            >
              {blocker.id}
            </button>
          ))}
        </div>
      ) : null}
      <footer>
        <span>{task.id}</span>
        <TaskDueTiming task={task} today={today} />
        {(task.objective_ids ?? []).slice(0, 2).map((objectiveId) => (
          onSelectObjective ? (
            <button
              aria-label={`Focus objective ${objectiveId}`}
              className="wsv-objective-link"
              key={objectiveId}
              onClick={(event) => {
                event.stopPropagation();
                onSelectObjective(objectiveId);
              }}
              type="button"
            >
              {objectiveId}
            </button>
          ) : <span key={objectiveId}>{objectiveId}</span>
        ))}
        {(task.objective_ids?.length ?? 0) > 2 ? <span>+{task.objective_ids!.length - 2}</span> : null}
        {task.context_count ? (
          <span title="Linked context">↗ {task.context_count}</span>
        ) : null}
        {subtaskProgress.total ? (
          <span
            className="wsv-subtask-progress"
            aria-label={`Steps for ${task.id}: ${subtaskProgress.done} of ${subtaskProgress.total} done`}
          >
            ✓ {subtaskProgress.done}/{subtaskProgress.total}
          </span>
        ) : null}
      </footer>
      <label className="wsv-status-action" onClick={(event) => event.stopPropagation()}>
        <span>{pending ? "Saving…" : "Move to"}</span>
        <select
          aria-label={`Change ${task.id} status`}
          value={status}
          disabled={pending || overlay}
          onChange={(event) => onChangeStatus(event.target.value as TaskStatus)}
        >
          {TASK_STATUSES.map((option) => (
            <option key={option} value={option}>
              {STATUS_LABELS[option]}
            </option>
          ))}
        </select>
      </label>
    </article>
  );
}

function BoardColumn({
  status,
  tasks,
  selectedTaskId,
  pendingIds,
  onSelectTask,
  onSelectObjective,
  blockersByTaskId,
  today,
  onChangeStatus,
}: {
  status: TaskStatus;
  tasks: readonly WorkspaceTask[];
  selectedTaskId?: string | null;
  pendingIds: ReadonlySet<string>;
  onSelectTask: (taskId: string) => void;
  onSelectObjective?: (objectiveId: string) => void;
  blockersByTaskId: ReadonlyMap<string, readonly TaskBlocker[]>;
  today: string;
  onChangeStatus: (task: WorkspaceTask, status: TaskStatus) => void;
}) {
  const droppable = useDroppable({ id: status });
  return (
    <section
      ref={droppable.setNodeRef}
      className={`wsv-board-column ${droppable.isOver ? "is-over" : ""}`}
      aria-labelledby={`wsv-column-${status}`}
    >
      <header className="wsv-board-column__header">
        <span className={`wsv-status-dot wsv-status-dot--${status}`} />
        <strong id={`wsv-column-${status}`}>{STATUS_LABELS[status]}</strong>
        <span className="wsv-count">{tasks.length}</span>
      </header>
      <div className="wsv-board-column__body">
        {tasks.map((task) => (
          <TaskCard
            key={task.id}
            task={task}
            status={status}
            selected={task.id === selectedTaskId}
            pending={pendingIds.has(task.id)}
            blockers={blockersByTaskId.get(task.id) ?? []}
            today={today}
            onSelect={() => onSelectTask(task.id)}
            onSelectTask={onSelectTask}
            onSelectObjective={onSelectObjective}
            onChangeStatus={(nextStatus) => onChangeStatus(task, nextStatus)}
          />
        ))}
        {!tasks.length ? <div className="wsv-column-empty">Drop work here</div> : null}
      </div>
    </section>
  );
}

export function BoardView({
  tasks,
  referenceTasks = tasks,
  selectedTaskId,
  onSelectTask,
  onSelectObjective,
  today: providedToday,
  onChangeTaskStatus,
}: BoardViewProps) {
  const localToday = useLocalToday();
  const today = providedToday ?? localToday;
  const [optimisticStatuses, setOptimisticStatuses] = useState<
    Record<string, TaskStatus>
  >({});
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const inFlightTaskIds = useRef(new Set<string>());
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor),
  );

  useEffect(() => {
    setOptimisticStatuses((current) => {
      const next = { ...current };
      let changed = false;
      for (const task of tasks) {
        if (next[task.id] && next[task.id] === asTaskStatus(task.status)) {
          delete next[task.id];
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [tasks]);

  const renderedTasks = useMemo(
    () =>
      tasks.map((task) => ({
        ...task,
        status: optimisticStatuses[task.id] || asTaskStatus(task.status),
      })),
    [tasks, optimisticStatuses],
  );
  const columns = useMemo(() => tasksByStatus(renderedTasks), [renderedTasks]);
  const taskById = useMemo(
    () => new Map(renderedTasks.map((task) => [task.id, task])),
    [renderedTasks],
  );
  const dependencyIndex = useMemo(
    () => indexDependencyTasks(referenceTasks),
    [referenceTasks],
  );
  const blockersByTaskId = useMemo(
    () => new Map(renderedTasks.map((task) => [
      task.id,
      blockingDependenciesFromIndex(dependencyIndex, task),
    ])),
    [dependencyIndex, renderedTasks],
  );

  const changeStatus = async (task: WorkspaceTask, nextStatus: TaskStatus) => {
    const previousStatus =
      optimisticStatuses[task.id] || asTaskStatus(task.status);
    if (previousStatus === nextStatus || inFlightTaskIds.current.has(task.id)) return;

    inFlightTaskIds.current.add(task.id);
    setError(null);
    setOptimisticStatuses((current) => ({ ...current, [task.id]: nextStatus }));
    setPendingIds((current) => new Set(current).add(task.id));
    try {
      await onChangeTaskStatus(task.id, nextStatus);
    } catch (reason) {
      setOptimisticStatuses((current) => {
        if (!(task.id in current)) return current;
        const next = { ...current };
        delete next[task.id];
        return next;
      });
      setError(
        reason instanceof Error
          ? reason.message
          : `Could not move ${task.id}. The card was restored.`,
      );
    } finally {
      inFlightTaskIds.current.delete(task.id);
      setPendingIds((current) => {
        const next = new Set(current);
        next.delete(task.id);
        return next;
      });
    }
  };

  const handleDragStart = (event: DragStartEvent) => {
    setActiveTaskId(String(event.active.id));
  };
  const handleDragEnd = (event: DragEndEvent) => {
    setActiveTaskId(null);
    if (!event.over) return;
    const task = taskById.get(String(event.active.id));
    const targetStatus = String(event.over.id) as TaskStatus;
    if (task && TASK_STATUSES.includes(targetStatus)) {
      void changeStatus(task, targetStatus);
    }
  };

  if (!tasks.length) {
    return (
      <div className="wsv-empty" role="status">
        <strong>No work matches these filters</strong>
        <span>The board will refill as soon as a filter is cleared.</span>
      </div>
    );
  }

  const activeTask = activeTaskId ? taskById.get(activeTaskId) : undefined;

  return (
    <div className="wsv-board-wrap">
      {error ? (
        <div className="wsv-board-error" role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)} aria-label="Dismiss error">
            ×
          </button>
        </div>
      ) : null}
      <DndContext
        sensors={sensors}
        collisionDetection={closestCorners}
        onDragStart={handleDragStart}
        onDragCancel={() => setActiveTaskId(null)}
        onDragEnd={handleDragEnd}
      >
        <div className="wsv-board">
          {TASK_STATUSES.map((status) => (
            <BoardColumn
              key={status}
              status={status}
              tasks={columns[status]}
              selectedTaskId={selectedTaskId}
              pendingIds={pendingIds}
              onSelectTask={onSelectTask}
              onSelectObjective={onSelectObjective}
              blockersByTaskId={blockersByTaskId}
              today={today}
              onChangeStatus={(task, nextStatus) => void changeStatus(task, nextStatus)}
            />
          ))}
        </div>
        <DragOverlay dropAnimation={null}>
          {activeTask ? (
            <TaskCard
              task={activeTask}
              status={asTaskStatus(activeTask.status)}
              selected={activeTask.id === selectedTaskId}
              pending={false}
              overlay
              blockers={blockersByTaskId.get(activeTask.id) ?? []}
              today={today}
              onSelect={() => undefined}
              onSelectTask={() => undefined}
              onSelectObjective={() => undefined}
              onChangeStatus={() => undefined}
            />
          ) : null}
        </DragOverlay>
      </DndContext>
    </div>
  );
}
