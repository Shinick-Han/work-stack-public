import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import {
  KeyboardSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";

import { TASK_STATUSES, type TaskStatus, type WorkspaceTask } from "./types";
import { asTaskStatus, tasksByStatus } from "./viewModels";
import {
  blockingDependenciesFromIndex,
  indexDependencyTasks,
  type TaskBlocker,
} from "../../../domain/taskRelationships";
import {
  BoardCardPointerSensor,
  boardKeyboardCoordinates,
  createBoardAnnouncements,
} from "./boardDnd";

interface BoardControllerInput {
  tasks: readonly WorkspaceTask[];
  referenceTasks: readonly WorkspaceTask[];
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>;
}

function dropConfirmedStatuses(
  current: Record<string, TaskStatus>,
  tasks: readonly WorkspaceTask[],
): Record<string, TaskStatus> {
  const next = { ...current };
  let changed = false;
  for (const task of tasks) {
    if (next[task.id] && next[task.id] === asTaskStatus(task.status)) {
      delete next[task.id];
      changed = true;
    }
  }
  return changed ? next : current;
}

function applyOptimisticStatuses(
  tasks: readonly WorkspaceTask[],
  optimisticStatuses: Record<string, TaskStatus>,
): WorkspaceTask[] {
  return tasks.map((task) => ({
    ...task,
    status: optimisticStatuses[task.id] || asTaskStatus(task.status),
  }));
}

function blockersByTask(
  renderedTasks: readonly WorkspaceTask[],
  dependencyIndex: ReturnType<typeof indexDependencyTasks>,
): Map<string, readonly TaskBlocker[]> {
  return new Map(
    renderedTasks.map((task) => [
      task.id,
      blockingDependenciesFromIndex(dependencyIndex, task),
    ]),
  );
}

function clearOptimisticStatus(
  current: Record<string, TaskStatus>,
  taskId: string,
): Record<string, TaskStatus> {
  if (!(taskId in current)) return current;
  const next = { ...current };
  delete next[taskId];
  return next;
}

function removePendingId(current: Set<string>, taskId: string): Set<string> {
  const next = new Set(current);
  next.delete(taskId);
  return next;
}

function statusChangeErrorMessage(reason: unknown, taskId: string): string {
  return reason instanceof Error
    ? reason.message
    : `Could not move ${taskId}. The card was restored.`;
}

async function commitBoardStatusChange(input: {
  task: WorkspaceTask;
  nextStatus: TaskStatus;
  optimisticStatuses: Record<string, TaskStatus>;
  inFlightTaskIds: { current: Set<string> };
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>;
  setError: Dispatch<SetStateAction<string | null>>;
  setOptimisticStatuses: Dispatch<SetStateAction<Record<string, TaskStatus>>>;
  setPendingIds: Dispatch<SetStateAction<Set<string>>>;
}): Promise<void> {
  const previousStatus =
    input.optimisticStatuses[input.task.id] || asTaskStatus(input.task.status);
  if (
    previousStatus === input.nextStatus ||
    input.inFlightTaskIds.current.has(input.task.id)
  ) {
    return;
  }

  const taskId = input.task.id;
  input.inFlightTaskIds.current.add(taskId);
  input.setError(null);
  input.setOptimisticStatuses((current) => ({ ...current, [taskId]: input.nextStatus }));
  input.setPendingIds((current) => new Set(current).add(taskId));
  try {
    await input.onChangeTaskStatus(taskId, input.nextStatus);
  } catch (reason) {
    input.setOptimisticStatuses((current) => clearOptimisticStatus(current, taskId));
    input.setError(statusChangeErrorMessage(reason, taskId));
  } finally {
    input.inFlightTaskIds.current.delete(taskId);
    input.setPendingIds((current) => removePendingId(current, taskId));
  }
}

function finishBoardDrag(
  event: DragEndEvent,
  taskById: ReadonlyMap<string, WorkspaceTask>,
  setActiveTaskId: Dispatch<SetStateAction<string | null>>,
  changeStatus: (task: WorkspaceTask, status: TaskStatus) => void,
) {
  setActiveTaskId(null);
  if (!event.over) return;
  const task = taskById.get(String(event.active.id));
  const targetStatus = String(event.over.id) as TaskStatus;
  if (task && TASK_STATUSES.includes(targetStatus)) {
    changeStatus(task, targetStatus);
  }
}

export function useBoardController({
  tasks,
  referenceTasks,
  onChangeTaskStatus,
}: BoardControllerInput) {
  const [optimisticStatuses, setOptimisticStatuses] = useState<
    Record<string, TaskStatus>
  >({});
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const inFlightTaskIds = useRef(new Set<string>());
  const sensors = useSensors(
    useSensor(BoardCardPointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: boardKeyboardCoordinates }),
  );

  useEffect(() => {
    setOptimisticStatuses((current) => dropConfirmedStatuses(current, tasks));
  }, [tasks]);

  const renderedTasks = useMemo(
    () => applyOptimisticStatuses(tasks, optimisticStatuses),
    [tasks, optimisticStatuses],
  );
  const columns = useMemo(() => tasksByStatus(renderedTasks), [renderedTasks]);
  const taskById = useMemo(
    () => new Map(renderedTasks.map((task) => [task.id, task])),
    [renderedTasks],
  );
  const announcements = useMemo(
    () => createBoardAnnouncements(taskById),
    [taskById],
  );
  const dependencyIndex = useMemo(
    () => indexDependencyTasks(referenceTasks),
    [referenceTasks],
  );
  const blockersByTaskId = useMemo(
    () => blockersByTask(renderedTasks, dependencyIndex),
    [dependencyIndex, renderedTasks],
  );

  const changeStatus = (task: WorkspaceTask, nextStatus: TaskStatus) => {
    void commitBoardStatusChange({
      task,
      nextStatus,
      optimisticStatuses,
      inFlightTaskIds,
      onChangeTaskStatus,
      setError,
      setOptimisticStatuses,
      setPendingIds,
    });
  };

  return {
    sensors,
    columns,
    taskById,
    announcements,
    blockersByTaskId,
    pendingIds,
    error,
    dismissError: () => setError(null),
    activeTaskId,
    handleDragStart: (event: DragStartEvent) => {
      setActiveTaskId(String(event.active.id));
    },
    handleDragCancel: () => setActiveTaskId(null),
    handleDragEnd: (event: DragEndEvent) => {
      finishBoardDrag(event, taskById, setActiveTaskId, changeStatus);
    },
    changeStatus,
  };
}
