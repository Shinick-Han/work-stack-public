import { useMemo, useState } from 'react'
import type { TaskStatus, WorkspaceTask } from './types'
import { STATUS_LABELS, taskSubtaskProgress } from './viewModels'
import { blockingDependencies } from '../../../domain/taskRelationships'
import { useLocalToday } from '../../focus/useLocalToday'
import { TaskDueTiming } from './TaskDueTiming'
import { readTablePreferences, writeTablePreferences, type TableDensity, type TableSortField } from './tablePreferences'

interface TableViewProps {
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  onSelectTask: (taskId: string) => void
  onSelectObjective?: (objectiveId: string) => void
  selectedTaskId?: string | null
  tasks: readonly WorkspaceTask[]
  referenceTasks?: readonly WorkspaceTask[]
  today?: string
}

export function TableView({ onChangeTaskStatus, onSelectTask, onSelectObjective, selectedTaskId, tasks, referenceTasks = tasks, today: providedToday }: TableViewProps) {
  const localToday = useLocalToday()
  const today = providedToday ?? localToday
  const [preferences, setPreferences] = useState(readTablePreferences)
  const { density, descending, sortField } = preferences
  const sorted = useMemo(() => [...tasks].sort((left, right) => {
    const leftValue = String(left[sortField] ?? '')
    const rightValue = String(right[sortField] ?? '')
    const result = leftValue.localeCompare(rightValue, undefined, { numeric: true })
    return descending ? -result : result
  }), [descending, sortField, tasks])

  const updatePreferences = (update: (current: typeof preferences) => typeof preferences) => {
    setPreferences((current) => writeTablePreferences(update(current)))
  }

  const changeSort = (field: TableSortField) => {
    updatePreferences((current) => field === current.sortField
      ? { ...current, descending: !current.descending }
      : { ...current, descending: false, sortField: field })
  }

  const changeDensity = (nextDensity: TableDensity) => {
    updatePreferences((current) => ({ ...current, density: nextDensity }))
  }

  return (
    <div className="wsv-table-wrap">
      <div className="wsv-table-toolbar">
        <span>Row density</span>
        <div aria-label="Table row density" role="group">
          <button aria-label="Comfortable rows" aria-pressed={density === 'comfortable'} onClick={() => changeDensity('comfortable')} type="button">Comfortable</button>
          <button aria-label="Compact rows" aria-pressed={density === 'compact'} onClick={() => changeDensity('compact')} type="button">Compact</button>
        </div>
        <small>On narrow screens, low-value technical columns collapse automatically.</small>
      </div>
      <table className={`wsv-table wsv-table--${density}`}>
        <caption className="sr-only">Tasks sorted by {sortField} {descending ? 'descending' : 'ascending'}</caption>
        <thead><tr>{([
          ['id', 'ID'], ['title', 'Task'], ['status', 'Status'], ['priority', 'Priority'], ['due', 'Due'],
        ] as const).map(([field, label]) => <th className={field === 'id' ? 'wsv-table-col--technical' : undefined} key={field} scope="col"><button aria-label={`Sort by ${label}`} onClick={() => changeSort(field)} type="button">{label}{sortField === field ? <span>{descending ? '↓' : '↑'}</span> : null}</button></th>)}<th scope="col">Readiness</th><th className="wsv-table-col--technical" scope="col">Steps</th><th scope="col">Objectives</th><th className="wsv-table-col--technical" scope="col">Context</th><th className="wsv-table-col--technical" scope="col">Rev</th></tr></thead>
        <tbody>{sorted.map((task) => {
          const status = (task.status ?? 'open') as TaskStatus
          const blockers = blockingDependencies(referenceTasks, task)
          const subtaskProgress = taskSubtaskProgress(task)
          return <tr className={task.id === selectedTaskId ? 'is-selected' : ''} key={task.id} onClick={() => onSelectTask(task.id)} tabIndex={0} onKeyDown={(event) => { if (event.target !== event.currentTarget) return; if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelectTask(task.id) } }}><td className="wsv-table-col--technical"><strong>{task.id}</strong></td><td><b>{task.title}</b><small>{task.tags?.join(' · ') || 'No tags'}</small></td><td><select aria-label={`Status for ${task.id}`} onClick={(event) => event.stopPropagation()} onChange={(event) => void onChangeTaskStatus(task.id, event.target.value as TaskStatus)} value={status}>{Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></td><td><span className={`wsv-priority wsv-priority--${(task.priority ?? 'P2').toLowerCase()}`}>{task.priority ?? 'P2'}</span></td><td><TaskDueTiming task={task} today={today} /></td><td>{blockers.length ? <span className="wsv-table-readiness wsv-readiness--blocked"><span>Blocked by</span>{blockers.map((blocker) => <button key={blocker.id} type="button" aria-label={`Blocked by ${blocker.id}${blocker.title ? `: ${blocker.title}` : ''}`} title={blocker.title ?? 'Dependency is missing from the current workspace'} disabled={!blocker.title} onClick={(event) => { event.stopPropagation(); onSelectTask(blocker.id) }}>{blocker.id}</button>)}</span> : <span className="wsv-table-readiness wsv-readiness--ready">Ready</span>}</td><td className="wsv-table-col--technical">{subtaskProgress.total ? <span className="wsv-subtask-progress" aria-label={`Steps for ${task.id}: ${subtaskProgress.done} of ${subtaskProgress.total} done`}>{subtaskProgress.done}/{subtaskProgress.total}</span> : '—'}</td><td><span className="wsv-objective-links">{task.objective_ids?.length ? task.objective_ids.map((objectiveId) => onSelectObjective ? <button aria-label={`Focus objective ${objectiveId}`} key={objectiveId} onClick={(event) => { event.stopPropagation(); onSelectObjective(objectiveId) }} type="button">{objectiveId}</button> : <span key={objectiveId}>{objectiveId}</span>) : '—'}</span></td><td className="wsv-table-col--technical">{task.context_count ?? 0}</td><td className="wsv-table-col--technical">{task.revision ?? 0}</td></tr>
        })}</tbody>
      </table>
      {!sorted.length ? <p className="wsv-table-empty">No Tasks match the current filters.</p> : null}
    </div>
  )
}
