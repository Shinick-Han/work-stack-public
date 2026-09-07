import type { ChangeEvent, ReactNode } from 'react'
import { Icon } from '../../components/Icon'
import {
  TASK_PRIORITIES,
  TASK_STATUSES,
  type AppUrlState,
  type WorkspaceProjection,
} from '../../domain/types'
import { getObjectiveTitle, statusLabels } from '../../utils/format'
import { decodeOutcomeFilter, type KeyResultNode, type OutcomeFilter } from './views/keyResultModel'
import { normalizeDoneVisibility, type DoneVisibility } from './views/completedTaskVisibility'

type SelectChange = (event: ChangeEvent<HTMLSelectElement>) => void

/** Every filter control renders the same labelled select shell: an sr-only
 * caption, the select itself carrying the identical accessible name, and the
 * shared chevron affordance. */
function FilterSelect({
  children,
  label,
  onChange,
  value,
}: {
  children: ReactNode
  label: string
  onChange: SelectChange
  value: string
}) {
  return (
    <label>
      <span className="sr-only">{label}</span>
      <select aria-label={label} onChange={onChange} value={value}>
        {children}
      </select>
      <Icon name="chevronDown" size={14} />
    </label>
  )
}

function TaskStateFilters({
  state,
  updateUrl,
}: {
  state: AppUrlState
  updateUrl: (patch: Partial<AppUrlState>, options?: { replace?: boolean }) => void
}) {
  return (
    <>
      <FilterSelect
        label="Filter by status"
        onChange={(event) => {
          const status = event.target.value as AppUrlState['status']
          // Explicit All is the one atomic status+visibility action.
          updateUrl(status === 'all'
            ? { status: 'all', doneVisibility: 'show' }
            : { status })
        }}
        value={state.status}
      >
        <option value="all">All statuses</option>
        {TASK_STATUSES.map((status) => <option key={status} value={status}>{statusLabels[status]}</option>)}
      </FilterSelect>
      <FilterSelect
        label="Completed task visibility"
        onChange={(event) => {
          const doneVisibility = event.target.value as DoneVisibility
          // Choosing hide while the status filter is Done would contradict
          // itself, so that one case moves status back to all atomically.
          updateUrl(doneVisibility === 'hide' && state.status === 'done'
            ? { doneVisibility, status: 'all' }
            : { doneVisibility })
        }}
        value={normalizeDoneVisibility(state.doneVisibility)}
      >
        <option value="default">Completed: hidden by default</option>
        <option value="hide">Completed: always hidden</option>
        <option value="show">Completed: shown</option>
      </FilterSelect>
      <FilterSelect
        label="Filter by priority"
        onChange={(event) => updateUrl({ priority: event.target.value as AppUrlState['priority'] })}
        value={state.priority}
      >
        <option value="all">All priorities</option>
        {TASK_PRIORITIES.map((priority) => <option key={priority} value={priority}>{priority}</option>)}
      </FilterSelect>
      <FilterSelect
        label="Filter by readiness"
        onChange={(event) => updateUrl({ readiness: event.target.value as AppUrlState['readiness'] })}
        value={state.readiness}
      >
        <option value="all">All readiness</option>
        <option value="ready">Ready to act</option>
        <option value="blocked">Blocked work</option>
      </FilterSelect>
    </>
  )
}

function TaskScopeFilters({
  objectives,
  outcomeNodes,
  outcomeValue,
  state,
  unresolvedSelected,
  updateUrl,
}: WorkspaceFilterPanelProps) {
  return (
    <>
      <FilterSelect
        label="Filter by due timing"
        onChange={(event) => updateUrl({ timing: event.target.value as AppUrlState['timing'] })}
        value={state.timing}
      >
        <option value="all">All due timing</option>
        <option value="overdue">Overdue</option>
        <option value="today">Due today</option>
        <option value="soon">Due soon</option>
        <option value="unscheduled">No due date</option>
      </FilterSelect>
      <FilterSelect
        label="Filter by objective"
        onChange={(event) => updateUrl({ objectiveId: event.target.value })}
        value={state.objectiveId}
      >
        <option value="all">All objectives</option>
        {objectives.map((objective) => (
          <option key={objective.id} value={objective.id}>
            {objective.id} · {getObjectiveTitle(objective)}
          </option>
        ))}
      </FilterSelect>
      <FilterSelect
        label="Filter by outcome"
        onChange={(event) => updateUrl({
          outcomeFilter: decodeOutcomeFilter(
            event.target.value === 'all' ? null : event.target.value,
          ),
        })}
        value={outcomeValue}
      >
        <option value="all">All outcomes</option>
        <option value="unassigned">Unassigned outcome</option>
        {outcomeNodes.map((node) => (
          <option
            key={node.key}
            value={JSON.stringify(['pair', node.objectiveId, node.keyResultId])}
          >
            {node.objectiveId} · {node.keyResultId} — {node.text}
          </option>
        ))}
        {unresolvedSelected ? (
          <option value={JSON.stringify(['pair', unresolvedSelected.objectiveId, unresolvedSelected.keyResultId])}>
            {unresolvedSelected.objectiveId} · {unresolvedSelected.keyResultId} — unresolved outcome
          </option>
        ) : null}
      </FilterSelect>
    </>
  )
}

interface WorkspaceFilterPanelProps {
  objectives: WorkspaceProjection['objectives']
  outcomeNodes: readonly KeyResultNode[]
  outcomeValue: string
  state: AppUrlState
  unresolvedSelected: Extract<OutcomeFilter, { kind: 'pair' }> | null
  updateUrl: (patch: Partial<AppUrlState>, options?: { replace?: boolean }) => void
}

/** The filter popover body. Components add no DOM of their own, so the emitted
 * markup is the same flat list of labelled selects it has always been. */
export function WorkspaceFilterPanel(props: WorkspaceFilterPanelProps) {
  return (
    <div className="filter-controls" id="workspace-filter-panel">
      <TaskStateFilters state={props.state} updateUrl={props.updateUrl} />
      <TaskScopeFilters {...props} />
    </div>
  )
}
