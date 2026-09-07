import { Pill } from '../../components/Primitives'
import type { AppUrlState } from '../../domain/types'
import { statusLabels } from '../../utils/format'
import type { CompletedVisibilityCounts } from './views/completedTaskVisibility'
import type { OutcomeFilter } from './views/keyResultModel'
import { readinessLabels, timingLabels } from './workspaceFilterLabels'

function ActiveFilterChip({
  clearLabel,
  label,
  onClear,
  tone,
}: {
  clearLabel: string
  label: string
  onClear: () => void
  tone: string
}) {
  return (
    <button aria-label={clearLabel} className="active-filter-chip" onClick={onClear} type="button">
      <Pill tone={tone}>{label}<span aria-hidden="true">×</span></Pill>
    </button>
  )
}

/** The one authoritative count line. It reads the same projection the canvas
 * renders in this render, so the two can never disagree. */
export function WorkspaceCountsSummary({
  counts,
  edgeCount,
}: {
  counts: CompletedVisibilityCounts
  edgeCount: number
}) {
  return (
    <span>
      {`${counts.visible} of ${counts.canonicalTotal}`} tasks shown
      {counts.hiddenCompleted
        ? ` · ${counts.hiddenCompleted} completed hidden`
        : ''}
      {counts.retained
        ? ` · ${counts.retained} kept open`
        : ''}
      {' · '}
      {edgeCount} canonical relationships
    </span>
  )
}

/** Dismissible chips for every non-default filter coordinate, plus the bulk
 * clear. Each chip patches exactly the coordinate it names. */
export function ActiveFilterChips({
  hasActiveFilters,
  outcomeFilter,
  state,
  updateUrl,
}: {
  hasActiveFilters: boolean
  outcomeFilter: OutcomeFilter
  state: AppUrlState
  updateUrl: (patch: Partial<AppUrlState>, options?: { replace?: boolean }) => void
}) {
  const selectedPair = outcomeFilter.kind === 'pair' ? outcomeFilter : null
  return (
    <>
      {state.search ? <ActiveFilterChip clearLabel={`Clear search filter ${state.search}`} label={`Search “${state.search}”`} onClear={() => updateUrl({ search: '' })} tone="neutral" /> : null}
      {state.objectiveId !== 'all' ? <ActiveFilterChip clearLabel={`Clear objective filter ${state.objectiveId}`} label={`Objective ${state.objectiveId}`} onClear={() => updateUrl({ objectiveId: 'all' })} tone="accent" /> : null}
      {state.status !== 'all' ? <ActiveFilterChip clearLabel={`Clear status filter ${statusLabels[state.status]}`} label={statusLabels[state.status]} onClear={() => updateUrl({ status: 'all' })} tone={state.status} /> : null}
      {state.priority !== 'all' ? <ActiveFilterChip clearLabel={`Clear priority filter ${state.priority}`} label={state.priority} onClear={() => updateUrl({ priority: 'all' })} tone={state.priority.toLowerCase()} /> : null}
      {state.readiness !== 'all' ? <ActiveFilterChip clearLabel={`Clear readiness filter ${readinessLabels[state.readiness]}`} label={readinessLabels[state.readiness]} onClear={() => updateUrl({ readiness: 'all' })} tone={state.readiness === 'blocked' ? 'warning' : 'success'} /> : null}
      {outcomeFilter.kind !== 'all' ? <ActiveFilterChip
        clearLabel="Clear outcome filter"
        label={selectedPair
          ? `Outcome ${selectedPair.objectiveId} · ${selectedPair.keyResultId}`
          : 'Unassigned outcome'}
        onClear={() => updateUrl({ outcomeFilter: { kind: 'all' } })}
        tone="accent"
      /> : null}
      {state.timing !== 'all' ? <ActiveFilterChip clearLabel={`Clear due timing filter ${timingLabels[state.timing]}`} label={timingLabels[state.timing]} onClear={() => updateUrl({ timing: 'all' })} tone={state.timing === 'overdue' ? 'warning' : state.timing === 'today' ? 'accent' : 'neutral'} /> : null}
      {hasActiveFilters ? (
        <button
          className="text-button"
          onClick={() => updateUrl({ search: '', status: 'all', priority: 'all', readiness: 'all', timing: 'all', objectiveId: 'all', outcomeFilter: { kind: 'all' } })}
          type="button"
        >Clear filters</button>
      ) : null}
    </>
  )
}
