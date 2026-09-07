import { type KeyboardEvent } from "react";
import { useDraggable, useDroppable } from "@dnd-kit/core";

import { TASK_STATUSES, type TaskStatus, type WorkspaceTask } from "./types";
import { STATUS_LABELS, asTaskPriority, asTaskStatus, taskSubtaskProgress } from "./viewModels";
import type { TaskBlocker } from "../../../domain/taskRelationships";
import { TaskDueTiming } from "./TaskDueTiming";
import type { OutcomeSelection } from "./KeyResultPresentation";
import type { KeyResultProjection } from "./keyResultModel";
import { BoardOutcomeBreadcrumb } from "./BoardOutcomeBreadcrumb";

export interface TaskCardProps {
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
  keyResultProjection?: KeyResultProjection;
  onSelectOutcome?: (selection: OutcomeSelection) => void;
  onChangeStatus: (status: TaskStatus) => void;
}

function taskCardClassName(
  selected: boolean,
  dragging: boolean,
  overlay: boolean,
  pending: boolean,
): string {
  return [
    "wsv-task-card",
    selected ? "is-selected" : "",
    dragging ? "is-dragging" : "",
    overlay ? "is-overlay" : "",
    !pending && !overlay ? "is-grabbable" : "",
  ]
    .filter(Boolean)
    .join(" ");
}

function onTaskCardKeyDown(
  event: KeyboardEvent<HTMLElement>,
  sensor: ((event: KeyboardEvent<HTMLElement>) => void) | undefined,
  onSelect: () => void,
) {
  if (typeof sensor === "function") sensor(event);
  if (event.target !== event.currentTarget) return;
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    onSelect();
  }
}

function asCardKeySensor(
  handler: unknown,
): ((event: KeyboardEvent<HTMLElement>) => void) | undefined {
  return typeof handler === "function"
    ? (handler as (event: KeyboardEvent<HTMLElement>) => void)
    : undefined;
}

function useBoardCardDrag(taskId: string, pending: boolean, overlay: boolean) {
  const draggable = useDraggable({
    id: overlay ? `overlay:${taskId}` : taskId,
    disabled: pending || overlay,
  });
  const transform = draggable.transform
    ? `translate3d(${draggable.transform.x}px, ${draggable.transform.y}px, 0)`
    : undefined;
  if (overlay) {
    return {
      isDragging: draggable.isDragging,
      transform,
      ref: undefined,
      listeners: {},
      activatorRef: undefined,
      attributes: {},
      sensor: undefined as ((event: KeyboardEvent<HTMLElement>) => void) | undefined,
    };
  }
  return {
    isDragging: draggable.isDragging,
    transform,
    ref: draggable.setNodeRef,
    listeners: draggable.listeners,
    activatorRef: draggable.setActivatorNodeRef,
    attributes: draggable.attributes,
    sensor: asCardKeySensor(draggable.listeners?.onKeyDown),
  };
}

function breadcrumbActions(
  overlay: boolean,
  onSelectObjective?: (objectiveId: string) => void,
  onSelectOutcome?: (selection: OutcomeSelection) => void,
) {
  if (overlay) return { onSelectObjective: undefined, onSelectOutcome: undefined };
  return { onSelectObjective, onSelectOutcome };
}

function TaskCardBlockers({
  taskId,
  blockers,
  overlay,
  onSelectTask,
}: {
  taskId: string;
  blockers: readonly TaskBlocker[];
  overlay: boolean;
  onSelectTask: (taskId: string) => void;
}) {
  if (!blockers.length) return null;
  return (
    <div className="wsv-readiness wsv-readiness--blocked" aria-label={`${taskId} readiness`}>
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
  );
}

function TaskCardFooter({
  task,
  today,
}: {
  task: WorkspaceTask;
  today: string;
}) {
  const subtaskProgress = taskSubtaskProgress(task);
  return (
    <footer>
      <span>{task.id}</span>
      <TaskDueTiming task={task} today={today} />
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
  );
}

function TaskCardStatusAction({
  taskId,
  status,
  pending,
  overlay,
  onChangeStatus,
}: {
  taskId: string;
  status: TaskStatus;
  pending: boolean;
  overlay: boolean;
  onChangeStatus: (status: TaskStatus) => void;
}) {
  return (
    <label className="wsv-status-action" onClick={(event) => event.stopPropagation()}>
      <span>{pending ? "Saving…" : "Move to"}</span>
      <select
        aria-label={`Change ${taskId} status`}
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
  );
}

export function TaskCard({
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
  keyResultProjection,
  onSelectOutcome,
  onChangeStatus,
}: TaskCardProps) {
  const drag = useBoardCardDrag(task.id, pending, overlay);
  const priority = asTaskPriority(task.priority);
  const outcomeActions = breadcrumbActions(overlay, onSelectObjective, onSelectOutcome);

  return (
    <article
      ref={drag.ref}
      className={taskCardClassName(selected, drag.isDragging, overlay, pending)}
      data-containment="board-card"
      data-priority={priority}
      style={{ transform: drag.transform }}
      {...drag.listeners}
      onClick={onSelect}
      onKeyDown={(event) => onTaskCardKeyDown(event, drag.sensor, onSelect)}
      tabIndex={0}
      aria-label={`${task.id}: ${task.title}`}
    >
      <header>
        <span className={`wsv-priority wsv-priority--${priority}`}>
          {priority}
        </span>
        <button
          ref={drag.activatorRef}
          className="wsv-drag-handle"
          type="button"
          aria-label={`Move ${task.title}`}
          title="Drag to another status"
          disabled={pending || overlay}
          onClick={(event) => event.stopPropagation()}
          {...drag.attributes}
        >
          ⠿
        </button>
      </header>
      <BoardOutcomeBreadcrumb
        task={task}
        keyResultProjection={keyResultProjection}
        onSelectObjective={outcomeActions.onSelectObjective}
        onSelectOutcome={outcomeActions.onSelectOutcome}
      />
      <h3>{task.title}</h3>
      <p>{task.detail || "No detail yet."}</p>
      <TaskCardBlockers
        taskId={task.id}
        blockers={blockers}
        overlay={overlay}
        onSelectTask={onSelectTask}
      />
      <TaskCardFooter task={task} today={today} />
      <TaskCardStatusAction
        taskId={task.id}
        status={status}
        pending={pending}
        overlay={overlay}
        onChangeStatus={onChangeStatus}
      />
    </article>
  );
}

export function BoardColumn({
  status,
  tasks,
  selectedTaskId,
  pendingIds,
  onSelectTask,
  onSelectObjective,
  keyResultProjection,
  onSelectOutcome,
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
  keyResultProjection?: KeyResultProjection;
  onSelectOutcome?: (selection: OutcomeSelection) => void;
  blockersByTaskId: ReadonlyMap<string, readonly TaskBlocker[]>;
  today: string;
  onChangeStatus: (task: WorkspaceTask, status: TaskStatus) => void;
}) {
  const droppable = useDroppable({ id: status });
  return (
    <section
      ref={droppable.setNodeRef}
      className={`wsv-board-column ${droppable.isOver ? "is-over" : ""}`}
      data-containment="board-column"
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
            keyResultProjection={keyResultProjection}
            onSelectOutcome={onSelectOutcome}
            onChangeStatus={(nextStatus) => onChangeStatus(task, nextStatus)}
          />
        ))}
        {!tasks.length ? <div className="wsv-column-empty">Drop work here</div> : null}
      </div>
    </section>
  );
}

export function BoardColumns({
  columns,
  selectedTaskId,
  pendingIds,
  onSelectTask,
  onSelectObjective,
  keyResultProjection,
  onSelectOutcome,
  blockersByTaskId,
  today,
  onChangeStatus,
}: {
  columns: Record<TaskStatus, readonly WorkspaceTask[]>;
  selectedTaskId?: string | null;
  pendingIds: ReadonlySet<string>;
  onSelectTask: (taskId: string) => void;
  onSelectObjective?: (objectiveId: string) => void;
  keyResultProjection?: KeyResultProjection;
  onSelectOutcome?: (selection: OutcomeSelection) => void;
  blockersByTaskId: ReadonlyMap<string, readonly TaskBlocker[]>;
  today: string;
  onChangeStatus: (task: WorkspaceTask, status: TaskStatus) => void;
}) {
  return (
    <div className="wsv-board" data-containment="board-track">
      {TASK_STATUSES.map((status) => (
        <BoardColumn
          key={status}
          status={status}
          tasks={columns[status]}
          selectedTaskId={selectedTaskId}
          pendingIds={pendingIds}
          onSelectTask={onSelectTask}
          onSelectObjective={onSelectObjective}
          keyResultProjection={keyResultProjection}
          onSelectOutcome={onSelectOutcome}
          blockersByTaskId={blockersByTaskId}
          today={today}
          onChangeStatus={onChangeStatus}
        />
      ))}
    </div>
  );
}

const overlayNoop = () => undefined;

export function BoardDragOverlay({
  task,
  selectedTaskId,
  blockersByTaskId,
  today,
  keyResultProjection,
}: {
  task: WorkspaceTask | undefined;
  selectedTaskId?: string | null;
  blockersByTaskId: ReadonlyMap<string, readonly TaskBlocker[]>;
  today: string;
  keyResultProjection?: KeyResultProjection;
}) {
  if (!task) return null;
  return (
    <TaskCard
      task={task}
      status={asTaskStatus(task.status)}
      selected={task.id === selectedTaskId}
      pending={false}
      overlay
      blockers={blockersByTaskId.get(task.id) ?? []}
      today={today}
      onSelect={overlayNoop}
      onSelectTask={overlayNoop}
      onSelectObjective={overlayNoop}
      keyResultProjection={keyResultProjection}
      onSelectOutcome={overlayNoop}
      onChangeStatus={overlayNoop}
    />
  );
}
