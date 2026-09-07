import { DndContext, DragOverlay } from "@dnd-kit/core";

import { type TaskStatus, type WorkspaceTask } from "./types";
import { useLocalToday } from "../../focus/useLocalToday";
import type { OutcomeSelection } from "./KeyResultPresentation";
import type { KeyResultProjection } from "./keyResultModel";
import { BOARD_DRAG_INSTRUCTIONS, boardStatusCollision } from "./boardDnd";
import { useBoardController } from "./boardController";
import { BoardColumns, BoardDragOverlay } from "./BoardTaskCard";

export {
  boardKeyboardCoordinates,
  boardStatusCollision,
  isBoardCardPointerBlocked,
} from "./boardDnd";

interface BoardViewProps {
  tasks: readonly WorkspaceTask[];
  referenceTasks?: readonly WorkspaceTask[];
  selectedTaskId?: string | null;
  onSelectTask: (taskId: string) => void;
  onSelectObjective?: (objectiveId: string) => void;
  keyResultProjection?: KeyResultProjection;
  onSelectOutcome?: (selection: OutcomeSelection) => void;
  today?: string;
  onChangeTaskStatus: (
    taskId: string,
    status: TaskStatus,
  ) => Promise<void>;
}

function BoardEmptyState() {
  return (
    <div className="wsv-empty" role="status">
      <strong>No work matches these filters</strong>
      <span>The board will refill as soon as a filter is cleared.</span>
    </div>
  );
}

function BoardStatusError({
  message,
  onDismiss,
}: {
  message: string;
  onDismiss: () => void;
}) {
  return (
    <div className="wsv-board-error" role="alert">
      <span>{message}</span>
      <button type="button" onClick={onDismiss} aria-label="Dismiss error">
        ×
      </button>
    </div>
  );
}

export function BoardView({
  tasks,
  referenceTasks = tasks,
  selectedTaskId,
  onSelectTask,
  onSelectObjective,
  keyResultProjection,
  onSelectOutcome,
  today: providedToday,
  onChangeTaskStatus,
}: BoardViewProps) {
  const localToday = useLocalToday();
  const today = providedToday ?? localToday;
  const board = useBoardController({ tasks, referenceTasks, onChangeTaskStatus });

  if (!tasks.length) {
    return <BoardEmptyState />;
  }

  const activeTask = board.activeTaskId ? board.taskById.get(board.activeTaskId) : undefined;

  return (
    <div className="wsv-board-wrap">
      {board.error ? (
        <BoardStatusError message={board.error} onDismiss={board.dismissError} />
      ) : null}
      <DndContext
        sensors={board.sensors}
        collisionDetection={boardStatusCollision}
        accessibility={{
          announcements: board.announcements,
          screenReaderInstructions: BOARD_DRAG_INSTRUCTIONS,
        }}
        onDragStart={board.handleDragStart}
        onDragCancel={board.handleDragCancel}
        onDragEnd={board.handleDragEnd}
      >
        <BoardColumns
          columns={board.columns}
          selectedTaskId={selectedTaskId}
          pendingIds={board.pendingIds}
          onSelectTask={onSelectTask}
          onSelectObjective={onSelectObjective}
          keyResultProjection={keyResultProjection}
          onSelectOutcome={onSelectOutcome}
          blockersByTaskId={board.blockersByTaskId}
          today={today}
          onChangeStatus={board.changeStatus}
        />
        <DragOverlay dropAnimation={null}>
          <BoardDragOverlay
            task={activeTask}
            selectedTaskId={selectedTaskId}
            blockersByTaskId={board.blockersByTaskId}
            today={today}
            keyResultProjection={keyResultProjection}
          />
        </DragOverlay>
      </DndContext>
    </div>
  );
}
