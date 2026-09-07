import type { RefObject } from 'react'
import { Icon } from '../../components/Icon'
import {
  WORKSPACE_VIEWS,
  type AppUrlState,
  type WorkspaceProjection,
} from '../../domain/types'
import type { KeyResultNode, OutcomeFilter } from './views/keyResultModel'
import { WorkspaceFilterPanel } from './WorkspaceFilterPanel'
import { viewMeta } from './workspaceFilterLabels'

/**
 * View switching, free-text search and the filter popover trigger. All of the
 * coordinates it reads and writes stay owned by the Page: this renders them and
 * hands every change straight back through updateUrl.
 */
export function WorkspaceToolbar({
  activeFilterCount,
  filterTriggerRef,
  filtersOpen,
  objectives,
  onToggleFilters,
  outcomeNodes,
  outcomeValue,
  state,
  unresolvedSelected,
  updateUrl,
}: {
  activeFilterCount: number
  filterTriggerRef: RefObject<HTMLButtonElement | null>
  filtersOpen: boolean
  objectives: WorkspaceProjection['objectives']
  onToggleFilters: () => void
  outcomeNodes: readonly KeyResultNode[]
  outcomeValue: string
  state: AppUrlState
  unresolvedSelected: Extract<OutcomeFilter, { kind: 'pair' }> | null
  updateUrl: (patch: Partial<AppUrlState>, options?: { replace?: boolean }) => void
}) {
  return (
    <div className="workspace-toolbar">
      <div className="view-tabs" aria-label="Workspace view" role="tablist">
        {WORKSPACE_VIEWS.map((view) => (
          <button
            aria-selected={state.view === view}
            className={state.view === view ? 'is-active' : ''}
            key={view}
            onClick={() => updateUrl({ view })}
            role="tab"
            type="button"
          >
            <Icon name={viewMeta[view].icon} size={16} />
            {viewMeta[view].label}
          </button>
        ))}
      </div>
      <label className="search-control">
        <span className="sr-only">Search tasks</span>
        <Icon name="search" size={16} />
        <input
          maxLength={200}
          onChange={(event) => updateUrl({ search: event.target.value }, { replace: true })}
          placeholder="Search tasks, tags, IDs…"
          type="search"
          value={state.search}
        />
        <kbd>/</kbd>
      </label>
      <div className="workspace-filter-menu">
        <button
          aria-controls="workspace-filter-panel"
          aria-expanded={filtersOpen}
          aria-label="Filter tasks"
          onClick={onToggleFilters}
          ref={filterTriggerRef}
          type="button"
        >
          <span>Filters</span>
          {activeFilterCount ? <strong>{activeFilterCount}</strong> : null}
          <Icon name="chevronDown" size={14} />
        </button>
        {filtersOpen ? (
          <WorkspaceFilterPanel
            objectives={objectives}
            outcomeNodes={outcomeNodes}
            outcomeValue={outcomeValue}
            state={state}
            unresolvedSelected={unresolvedSelected}
            updateUrl={updateUrl}
          />
        ) : null}
      </div>
    </div>
  )
}
