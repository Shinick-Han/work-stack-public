import { useLocalToday } from '../../focus/useLocalToday'
import type { TaskStatus, WorkspaceTask } from './types'
import type { OutcomeSelection } from './KeyResultPresentation'
import type { KeyResultProjection } from './keyResultModel'
import { TableHead, TableToolbar } from './TableChrome'
import { TableTaskRow } from './TableTaskRow'
import { useTableViewSession } from './useTableViewSession'
import './TableView.drag.css'

interface TableViewProps {
  workspaceId?: string
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  onSelectTask: (taskId: string) => void
  onSelectObjective?: (objectiveId: string) => void
  selectedTaskId?: string | null
  keyResultProjection?: KeyResultProjection
  onSelectOutcome?: (selection: OutcomeSelection) => void
  tasks: readonly WorkspaceTask[]
  referenceTasks?: readonly WorkspaceTask[]
  today?: string
}

export function TableView({
  workspaceId,
  onChangeTaskStatus,
  onSelectTask,
  onSelectObjective,
  selectedTaskId,
  keyResultProjection,
  onSelectOutcome,
  tasks,
  referenceTasks = tasks,
  today: providedToday,
}: TableViewProps) {
  const localToday = useLocalToday()
  const today = providedToday ?? localToday
  const session = useTableViewSession({ workspaceId, tasks, referenceTasks })
  return (
    <div className="wsv-table-wrap">
      <div className="wsv-table-live" aria-live="polite" aria-atomic="true">{session.announcement}</div>
      <TableToolbar
        density={session.density}
        manual={session.manual}
        onChangeDensity={session.changeDensity}
        onManual={session.changeToManual}
        onReset={session.resetTableOrder}
      />
      <table className={`wsv-table wsv-table--${session.density}`}>
        <caption className="sr-only">{session.caption}</caption>
        <TableHead
          sortField={session.sortField}
          descending={session.descending}
          onChangeSort={session.changeSort}
        />
        <tbody>{session.displayed.map((task) => (
          <TableTaskRow
            key={task.id}
            task={task}
            today={today}
            selected={task.id === selectedTaskId}
            manual={session.manual}
            liveGestureId={session.liveGesture?.id ?? null}
            referenceTasks={referenceTasks}
            keyResultProjection={keyResultProjection}
            onSelectTask={onSelectTask}
            onSelectObjective={onSelectObjective}
            onSelectOutcome={onSelectOutcome}
            onChangeTaskStatus={onChangeTaskStatus}
            gestureRef={session.gestureRef}
            onMoveKeyDown={session.handleMoveKeyDown}
            onHandlePointerDown={session.onHandlePointerDown}
            onHandlePointerMove={session.onHandlePointerMove}
            onHandlePointerUp={session.onHandlePointerUp}
            onHandlePointerCancel={session.onHandlePointerCancel}
          />
        ))}</tbody>
      </table>
      {!session.displayed.length ? <p className="wsv-table-empty">No Tasks match the current filters.</p> : null}
    </div>
  )
}
