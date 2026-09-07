import type { KeyboardEvent, MutableRefObject } from 'react'
import type { TaskStatus, WorkspaceTask } from './types'
import { STATUS_LABELS, taskSubtaskProgress } from './viewModels'
import { blockingDependencies, type TaskBlocker } from '../../../domain/taskRelationships'
import { TaskDueTiming } from './TaskDueTiming'
import { tableMoveHandleLabel } from './tableOrdering'
import { type OutcomeSelection } from './KeyResultPresentation'
import { outcomeChipsForTask, type OutcomeChip } from './keyResultViewModel'
import type { KeyResultProjection } from './keyResultModel'
import type { HandlePointerEvent, TableGesture } from './tableReorderGesture'

function stopRowActivation(event: { stopPropagation: () => void }) {
  event.stopPropagation()
}

function uniqueInOrder(ids: readonly string[]): string[] {
  const seen = new Set<string>()
  const ordered: string[] = []
  for (const id of ids) {
    if (seen.has(id)) continue
    seen.add(id)
    ordered.push(id)
  }
  return ordered
}

function TableObjectiveCell({
  objectiveIds,
  onSelectObjective,
}: {
  objectiveIds: readonly string[]
  onSelectObjective?: (objectiveId: string) => void
}) {
  if (!objectiveIds.length) {
    return <span className="wsv-table-outcome-state">Unaligned</span>
  }
  return (
    <span className="wsv-objective-links">
      {objectiveIds.map((objectiveId) => (
        onSelectObjective ? (
          <button
            aria-label={`Focus objective ${objectiveId}`}
            key={objectiveId}
            onClick={(event) => {
              stopRowActivation(event)
              onSelectObjective(objectiveId)
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') stopRowActivation(event)
            }}
            type="button"
          >
            {objectiveId}
          </button>
        ) : <span key={objectiveId}>{objectiveId}</span>
      ))}
    </span>
  )
}

function TableKeyResultControl({
  chip,
  onSelectOutcome,
}: {
  chip: OutcomeChip
  onSelectOutcome?: (selection: OutcomeSelection) => void
}) {
  const body = (
    <>
      <strong>{chip.keyResultId}</strong>
      <span>{chip.resolved ? chip.text : 'Unresolved outcome'}</span>
    </>
  )
  if (!onSelectOutcome) {
    return (
      <span className={chip.resolved ? 'wsv-table-kr' : 'wsv-table-kr wsv-table-kr--unresolved'}>
        {body}
      </span>
    )
  }
  return (
    <button
      aria-label={`Filter by outcome ${chip.objectiveId} ${chip.keyResultId}`}
      className={chip.resolved ? 'wsv-table-kr wsv-table-kr--action' : 'wsv-table-kr wsv-table-kr--action wsv-table-kr--unresolved'}
      onClick={(event) => {
        stopRowActivation(event)
        onSelectOutcome({
          objectiveId: chip.objectiveId,
          keyResultId: chip.keyResultId,
        })
      }}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') stopRowActivation(event)
      }}
      type="button"
    >
      {body}
    </button>
  )
}

function TableKeyResultCell({
  chips,
  hasObjective,
  onSelectOutcome,
}: {
  chips: readonly OutcomeChip[]
  hasObjective: boolean
  onSelectOutcome?: (selection: OutcomeSelection) => void
}) {
  if (chips.length) {
    return (
      <span className="wsv-table-kr-list">
        {chips.map((chip) => (
          <TableKeyResultControl chip={chip} key={chip.key} onSelectOutcome={onSelectOutcome} />
        ))}
      </span>
    )
  }
  if (hasObjective) {
    return <span className="wsv-table-outcome-state">Objective-only</span>
  }
  return <span className="wsv-table-outcome-state">Unassigned outcome</span>
}

function TableReadinessCell({
  blockers,
  onSelectTask,
}: {
  blockers: readonly TaskBlocker[]
  onSelectTask: (taskId: string) => void
}) {
  if (!blockers.length) {
    return <td><span className="wsv-table-readiness wsv-readiness--ready">Ready</span></td>
  }
  return (
    <td>
      <span className="wsv-table-readiness wsv-readiness--blocked">
        <span>Blocked by</span>
        {blockers.map((blocker) => (
          <button
            key={blocker.id}
            type="button"
            aria-label={`Blocked by ${blocker.id}${blocker.title ? `: ${blocker.title}` : ''}`}
            title={blocker.title ?? 'Dependency is missing from the current workspace'}
            disabled={!blocker.title}
            onClick={(event) => { event.stopPropagation(); onSelectTask(blocker.id) }}
          >
            {blocker.id}
          </button>
        ))}
      </span>
    </td>
  )
}

function TableMoveHandle({
  taskId,
  manual,
  pressed,
  onMoveKeyDown,
  onHandlePointerDown,
  onHandlePointerMove,
  onHandlePointerUp,
  onHandlePointerCancel,
}: {
  taskId: string
  manual: boolean
  pressed: boolean
  onMoveKeyDown: (id: string, event: KeyboardEvent<HTMLButtonElement>) => void
  onHandlePointerDown: (id: string, event: HandlePointerEvent) => void
  onHandlePointerMove: (event: HandlePointerEvent) => void
  onHandlePointerUp: (event: HandlePointerEvent) => void
  onHandlePointerCancel: (event: HandlePointerEvent) => void
}) {
  return (
    <button
      aria-label={tableMoveHandleLabel(taskId, manual)}
      aria-pressed={pressed}
      className="wsv-table-move-handle"
      data-table-move-handle={taskId}
      disabled={!manual}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => onMoveKeyDown(taskId, event)}
      onMouseDown={(event) => onHandlePointerDown(taskId, event)}
      onMouseMove={onHandlePointerMove}
      onMouseUp={onHandlePointerUp}
      onPointerCancel={onHandlePointerCancel}
      onPointerDown={(event) => onHandlePointerDown(taskId, event)}
      onPointerMove={onHandlePointerMove}
      onPointerUp={onHandlePointerUp}
      type="button"
    >
      ⠿
    </button>
  )
}

function activateTableRow(event: KeyboardEvent<HTMLTableRowElement>, taskId: string, onSelectTask: (taskId: string) => void) {
  if (event.target !== event.currentTarget) return
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  onSelectTask(taskId)
}

export type TableTaskRowProps = {
  task: WorkspaceTask
  today: string
  selected: boolean
  manual: boolean
  liveGestureId: string | null
  referenceTasks: readonly WorkspaceTask[]
  keyResultProjection?: KeyResultProjection
  onSelectTask: (taskId: string) => void
  onSelectObjective?: (objectiveId: string) => void
  onSelectOutcome?: (selection: OutcomeSelection) => void
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  gestureRef: MutableRefObject<TableGesture | null>
  onMoveKeyDown: (id: string, event: KeyboardEvent<HTMLButtonElement>) => void
  onHandlePointerDown: (id: string, event: HandlePointerEvent) => void
  onHandlePointerMove: (event: HandlePointerEvent) => void
  onHandlePointerUp: (event: HandlePointerEvent) => void
  onHandlePointerCancel: (event: HandlePointerEvent) => void
}

function tableRowModel(
  task: WorkspaceTask,
  referenceTasks: readonly WorkspaceTask[],
  keyResultProjection?: KeyResultProjection,
) {
  const chips = keyResultProjection ? outcomeChipsForTask(keyResultProjection, task.id).chips : []
  return {
    status: (task.status ?? 'open') as TaskStatus,
    blockers: blockingDependencies(referenceTasks, task),
    subtaskProgress: taskSubtaskProgress(task),
    chips,
    objectiveIds: uniqueInOrder([
      ...chips.map((chip) => chip.objectiveId),
      ...(task.objective_ids ?? []),
    ]),
  }
}

export function TableTaskRow({
  task,
  today,
  selected,
  manual,
  liveGestureId,
  referenceTasks,
  keyResultProjection,
  onSelectTask,
  onSelectObjective,
  onSelectOutcome,
  onChangeTaskStatus,
  gestureRef,
  onMoveKeyDown,
  onHandlePointerDown,
  onHandlePointerMove,
  onHandlePointerUp,
  onHandlePointerCancel,
}: TableTaskRowProps) {
  const { status, blockers, subtaskProgress, chips, objectiveIds } = tableRowModel(
    task, referenceTasks, keyResultProjection,
  )
  return (
    <tr
      className={selected ? 'is-selected' : ''}
      data-task-id={task.id}
      onClick={() => {
        if (gestureRef.current) return
        onSelectTask(task.id)
      }}
      tabIndex={0}
      onKeyDown={(event) => activateTableRow(event, task.id, onSelectTask)}
    >
      <td className="wsv-table-col--technical"><strong>{task.id}</strong></td>
      <td>
        <div className="wsv-table-title-row">
          <TableMoveHandle
            taskId={task.id}
            manual={manual}
            pressed={liveGestureId === task.id}
            onMoveKeyDown={onMoveKeyDown}
            onHandlePointerDown={onHandlePointerDown}
            onHandlePointerMove={onHandlePointerMove}
            onHandlePointerUp={onHandlePointerUp}
            onHandlePointerCancel={onHandlePointerCancel}
          />
          <span>
            <b>{task.title}</b>
            <small>{task.tags?.join(' · ') || 'No tags'}</small>
          </span>
        </div>
      </td>
      <td>
        <select
          aria-label={`Status for ${task.id}`}
          onClick={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
          onChange={(event) => void onChangeTaskStatus(task.id, event.target.value as TaskStatus)}
          value={status}
        >
          {Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </td>
      <td><span className={`wsv-priority wsv-priority--${(task.priority ?? 'P2').toLowerCase()}`}>{task.priority ?? 'P2'}</span></td>
      <td><TaskDueTiming task={task} today={today} /></td>
      <TableReadinessCell blockers={blockers} onSelectTask={onSelectTask} />
      <td className="wsv-table-col--technical">
        {subtaskProgress.total ? (
          <span className="wsv-subtask-progress" aria-label={`Steps for ${task.id}: ${subtaskProgress.done} of ${subtaskProgress.total} done`}>
            {subtaskProgress.done}/{subtaskProgress.total}
          </span>
        ) : '—'}
      </td>
      <td>
        <TableObjectiveCell objectiveIds={objectiveIds} onSelectObjective={onSelectObjective} />
      </td>
      <td>
        <TableKeyResultCell
          chips={chips}
          hasObjective={objectiveIds.length > 0}
          onSelectOutcome={onSelectOutcome}
        />
      </td>
      <td className="wsv-table-col--technical">{task.context_count ?? 0}</td>
      <td className="wsv-table-col--technical">{task.revision ?? 0}</td>
    </tr>
  )
}
