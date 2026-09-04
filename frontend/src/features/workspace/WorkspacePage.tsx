import { WorkspaceViews } from 'features/workspace/views'
import { KeyResultCatalog } from './views/KeyResultPresentation'
import { Button, EmptyState, IconButton, Pill } from '../../components/Primitives'
import { Icon } from '../../components/Icon'
import {
  TASK_PRIORITIES,
  TASK_STATUSES,
  WORKSPACE_VIEWS,
  type AppUrlState,
  type TaskStatus,
  type WorkspaceProjection,
} from '../../domain/types'
import { getObjectiveTitle, statusLabels } from '../../utils/format'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  decodeOutcomeFilter,
  encodeOutcomeFilter,
  normalizeOutcomeFilter,
  projectKeyResults,
} from './views/keyResultModel'
import {
  filterCoordinates,
  readSavedFilters,
  sameSavedFilter,
  writeSavedFilters,
} from './savedFilters'
import { filterWorkspaceTasks } from './views/viewModels'
import {
  normalizeDoneVisibility,
  projectCompletedTaskVisibility,
  completedVisibilityScopeKey,
  type CompletedVisibilityReveal,
  type DoneVisibility,
} from './views/completedTaskVisibility'
import { TaskPrerequisiteContext } from './views/TaskPrerequisiteContext'
import { useLocalToday } from '../focus/useLocalToday'

type SavedViewEditor = {
  mode: 'create' | 'rename'
  name: string
}

interface WorkspacePageProps {
  workspace: WorkspaceProjection
  state: AppUrlState
  updateUrl: (patch: Partial<AppUrlState>, options?: { replace?: boolean }) => void
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  onCreateTask: () => void
  onOpenObjectives: () => void
  onRefresh: () => void
  isRefreshing: boolean
}

const viewMeta = {
  graph: { label: 'Graph', icon: 'graph' as const, description: 'See alignment and dependencies' },
  board: { label: 'Board', icon: 'board' as const, description: 'Move work through its flow' },
  treemap: { label: 'Treemap', icon: 'treemap' as const, description: 'Read effort by objective' },
  table: { label: 'Table', icon: 'table' as const, description: 'Scan and sort planning facts' },
}

const readinessLabels = {
  all: 'All readiness',
  ready: 'Ready to act',
  blocked: 'Blocked work',
} as const

const timingLabels = {
  all: 'All due timing',
  overdue: 'Overdue',
  today: 'Due today',
  soon: 'Due soon',
  unscheduled: 'No due date',
} as const

function suggestedSavedViewName(state: AppUrlState): string {
  const parts = [
    state.search ? `“${state.search.slice(0, 36)}”` : '',
    state.status !== 'all' ? statusLabels[state.status] : '',
    state.priority !== 'all' ? state.priority : '',
    state.objectiveId !== 'all' ? state.objectiveId : '',
    state.readiness !== 'all' ? readinessLabels[state.readiness] : '',
    state.timing !== 'all' ? timingLabels[state.timing] : '',
    viewMeta[state.view].label,
  ].filter(Boolean)
  return parts.join(' · ') || 'All Tasks'
}

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

export function WorkspacePage({
  isRefreshing,
  onChangeTaskStatus,
  onCreateTask,
  onOpenObjectives,
  onRefresh,
  state,
  updateUrl,
  workspace,
}: WorkspacePageProps) {
  const today = useLocalToday()
  const active = workspace.tasks.filter((task) => task.status === 'open' || task.status === 'started')
  const completed = workspace.tasks.filter((task) => task.status === 'done').length
  const critical = active.filter((task) => task.priority === 'P0').length
  const blocked = useMemo(
    () => filterWorkspaceTasks(workspace.tasks, { readiness: 'blocked' }).length,
    [workspace.tasks],
  )
  const aligned = workspace.tasks.filter((task) => task.objective_ids.length > 0).length
  const selectedView = viewMeta[state.view]
  // The URL state type keeps the coordinate optional for compatibility; the
  // Page narrows it once so every reader below sees a concrete value.
  const outcomeFilter = normalizeOutcomeFilter(state.outcomeFilter)
  const hasActiveFilters = Boolean(
    state.search
    || state.status !== 'all'
    || state.priority !== 'all'
    || state.readiness !== 'all'
    || state.timing !== 'all'
    || state.objectiveId !== 'all'
    || outcomeFilter.kind !== 'all',
  )
  // One memoized options projection for this render; Slice B reuses the same
  // call with the projection's visibleTasks.
  const activeFilterCount = [
    state.status !== 'all',
    state.priority !== 'all',
    state.readiness !== 'all',
    state.timing !== 'all',
    state.objectiveId !== 'all',
    outcomeFilter.kind !== 'all',
  ].filter(Boolean).length
  // Transient Graph interaction identities. They live only here, never in the
  // URL, storage, Task fields or saved filters.
  const [contextTargetTaskId, setContextTargetTaskId] = useState<string | null>(null)
  const [focusPinnedTaskId, setFocusPinnedTaskId] = useState<string | null>(null)

  /**
   * Focus must never be dropped on the document body when a view-local surface
   * goes away. This hands it to the active view tab, which survives both a view
   * change and canonical deletion, and only when focus was actually lost.
   */
  const handOffLostFocus = useCallback((force = false) => {
    if (typeof document === 'undefined') return
    // A normal click that already focused a surviving control is left alone;
    // canonical deletion forces the handoff because its target is gone.
    if (!force && document.activeElement && document.activeElement !== document.body) return
    const fallback = document.querySelector<HTMLElement>('[data-workspace-focus-fallback]')
    if (!fallback) return
    if (!fallback.hasAttribute('tabindex')) fallback.setAttribute('tabindex', '-1')
    fallback.focus()
  }, [])

  // Leaving Graph releases its view-local popup and focus identities after a
  // deliberate handoff. This runs for externally updated view state and history
  // too, not only the view click handler. The shell selection is untouched.
  useEffect(() => {
    if (state.view === 'graph') return
    if (!contextTargetTaskId && !focusPinnedTaskId) return
    setContextTargetTaskId(null)
    setFocusPinnedTaskId(null)
    handOffLostFocus()
  }, [state.view, contextTargetTaskId, focusPinnedTaskId, handOffLostFocus])

  // A pinned Task that leaves the canonical set entirely releases its reasons;
  // completion or visibility filtering alone never reaches here.
  const canonicalIds = useMemo(
    () => new Set(workspace.tasks.map((item) => item.id)),
    [workspace.tasks],
  )
  useEffect(() => {
    const staleContext = contextTargetTaskId && !canonicalIds.has(contextTargetTaskId)
    const staleFocus = focusPinnedTaskId && !canonicalIds.has(focusPinnedTaskId)
    if (!staleContext && !staleFocus) return
    if (staleContext) setContextTargetTaskId(null)
    if (staleFocus) setFocusPinnedTaskId(null)
    handOffLostFocus(true)
  }, [canonicalIds, contextTargetTaskId, focusPinnedTaskId, handOffLostFocus])

  /**
   * The single authoritative projection. The summary and every renderer read
   * this same value in the same render, so no second filter or effect-delayed
   * count reconciliation can disagree with the canvas.
   */
  // View-local reasons belong to Graph. Scoping them here, rather than only in
  // a passive effect, keeps the FIRST committed tree of a new view correct.
  const activeContextTargetTaskId = state.view === 'graph' ? contextTargetTaskId : null
  const activeFocusPinnedTaskId = state.view === 'graph' ? focusPinnedTaskId : null

  // Frozen routine decision: an actually open Graph context popup is the
  // prerequisite anchor, independently of shell selection; otherwise the
  // selected Task; otherwise there is no anchor.
  const prerequisiteAnchorTaskId = activeContextTargetTaskId ?? state.taskId ?? null

  // Transient reveal request. It is never written to the URL, storage or a Task,
  // and the projection normalizes it against the current scope and anchor, so a
  // stale request is excluded in the FIRST committed render rather than later.
  const [revealRequest, setRevealRequest] = useState<CompletedVisibilityReveal | null>(null)

  const projection = useMemo(() => projectCompletedTaskVisibility({
    tasks: workspace.tasks,
    filters: {
      search: state.search,
      status: state.status,
      priority: state.priority,
      readiness: state.readiness,
      timing: state.timing,
      today,
      objectiveId: state.objectiveId,
      outcome: state.outcomeFilter,
    },
    view: state.view,
    doneVisibility: state.doneVisibility,
    selectedTaskId: state.taskId,
    contextTargetTaskId: activeContextTargetTaskId,
    focusPinnedTaskId: activeFocusPinnedTaskId,
    prerequisiteAnchorTaskId,
    reveal: revealRequest,
  }), [
    state.objectiveId,
    state.outcomeFilter,
    state.priority,
    state.readiness,
    state.timing,
    state.search,
    state.status,
    state.view,
    state.doneVisibility,
    state.taskId,
    activeContextTargetTaskId,
    activeFocusPinnedTaskId,
    prerequisiteAnchorTaskId,
    revealRequest,
    today,
    workspace.tasks,
  ])

  // Adopt the normalized reveal so a target dropped from eligibility cannot
  // reappear on its own if it becomes Done again later.
  const normalizedReveal = projection.reveal
  useEffect(() => {
    setRevealRequest((current) => {
      if (current === normalizedReveal) return current
      if (!current && !normalizedReveal) return current
      if (
        current
        && normalizedReveal
        && current.anchorTaskId === normalizedReveal.anchorTaskId
        && current.scopeKey === normalizedReveal.scopeKey
        && current.taskIds.length === normalizedReveal.taskIds.length
        && current.taskIds.every((id, index) => id === normalizedReveal.taskIds[index])
      ) {
        return current
      }
      return normalizedReveal
    })
  }, [normalizedReveal])

  const revealPrerequisites = (taskIds: readonly string[]) => {
    if (!prerequisiteAnchorTaskId) return
    setRevealRequest({
      anchorTaskId: prerequisiteAnchorTaskId,
      scopeKey: revealScopeKey,
      taskIds,
    })
  }

  const revealScopeKey = completedVisibilityScopeKey(
    state.view,
    {
      search: state.search,
      status: state.status,
      priority: state.priority,
      readiness: state.readiness,
      timing: state.timing,
      today,
      objectiveId: state.objectiveId,
      outcome: state.outcomeFilter,
    },
    normalizeDoneVisibility(state.doneVisibility),
  )
  // ONE Page-owned projector for this render: the options list, the shared
  // catalog and every renderer read the same projection.
  const keyResultProjection = useMemo(
    () => projectKeyResults({
      workspaceId: workspace.workspace.id,
      tasks: workspace.tasks,
      // The canonical Tasks that the existing completed projection kept visible.
      visibleTasks: workspace.tasks.filter(
        (item) => projection.visibleTasks.some((visible) => visible.id === item.id),
      ),
      objectives: workspace.objectives,
    }),
    [workspace.workspace.id, workspace.tasks, workspace.objectives, projection.visibleTasks],
  )
  // Outcomes exist independently of Tasks: an Objective with key results is
  // enough to render a useful Graph even before the first Task.
  const hasOutcomeCatalog = keyResultProjection.keyResults.length > 0
  const outcomeNodes = keyResultProjection.keyResults
  const outcomeValue = encodeOutcomeFilter(outcomeFilter) ?? 'all'
  const selectedPair = outcomeFilter.kind === 'pair' ? outcomeFilter : null
  // A stored pair with no uniquely resolvable node stays filterable as a
  // reference rather than becoming Unassigned or a fabricated KR node.
  const unresolvedSelected = selectedPair && !outcomeNodes.some(
    (node) => node.objectiveId === selectedPair.objectiveId
      && node.keyResultId === selectedPair.keyResultId,
  )
    ? selectedPair
    : null
  const selectOutcome = (selection: { objectiveId: string; keyResultId: string }) => updateUrl({
    outcomeFilter: { kind: 'pair', ...selection },
  })
  const [savedFilters, setSavedFilters] = useState(readSavedFilters)
  const [filtersOpen, setFiltersOpen] = useState(false)
  const filterTriggerRef = useRef<HTMLButtonElement>(null)
  const [activeSavedFilterId, setActiveSavedFilterId] = useState<string | null>(null)
  const [savedViewEditor, setSavedViewEditor] = useState<SavedViewEditor | null>(null)
  const matchingSavedFilter = savedFilters.find((filter) => sameSavedFilter(filter, state)) ?? null
  const selectedSavedFilter = savedFilters.find((filter) => filter.id === activeSavedFilterId)
    ?? matchingSavedFilter
  const selectedSavedFilterChanged = Boolean(selectedSavedFilter && !sameSavedFilter(selectedSavedFilter, state))

  useEffect(() => {
    if (!filtersOpen) return
    const closeFilters = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setFiltersOpen(false)
      window.requestAnimationFrame(() => filterTriggerRef.current?.focus())
    }
    window.addEventListener('keydown', closeFilters)
    return () => window.removeEventListener('keydown', closeFilters)
  }, [filtersOpen])

  const createSavedFilter = (name: string) => {
    const id = typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `${Date.now()}:${Math.random().toString(36).slice(2)}`
    setSavedFilters(writeSavedFilters([...savedFilters, {
      id,
      name,
      ...filterCoordinates(state),
    }]))
    setActiveSavedFilterId(id)
  }
  const applySavedFilter = (filterId: string) => {
    const filter = savedFilters.find((candidate) => candidate.id === filterId)
    if (!filter) {
      setActiveSavedFilterId(null)
      return
    }
    setActiveSavedFilterId(filter.id)
    setSavedViewEditor(null)
    updateUrl({ ...filterCoordinates({ ...state, ...filter }), taskId: null })
  }
  const submitSavedViewName = () => {
    if (!savedViewEditor) return
    const name = savedViewEditor.name.trim().replace(/\s+/g, ' ').slice(0, 120)
    if (!name) return
    if (savedViewEditor.mode === 'create') {
      createSavedFilter(name)
    } else if (selectedSavedFilter) {
      setSavedFilters(writeSavedFilters(savedFilters.map((filter) => (
        filter.id === selectedSavedFilter.id ? { ...filter, name } : filter
      ))))
      setActiveSavedFilterId(selectedSavedFilter.id)
    }
    setSavedViewEditor(null)
  }
  const updateSelectedFilter = () => {
    if (!selectedSavedFilter || !selectedSavedFilterChanged) return
    setSavedFilters(writeSavedFilters(savedFilters.map((filter) => (
      filter.id === selectedSavedFilter.id
        ? { ...filter, ...filterCoordinates(state) }
        : filter
    ))))
  }
  const removeSelectedFilter = () => {
    if (!selectedSavedFilter) return
    setSavedFilters(writeSavedFilters(savedFilters.filter((filter) => filter.id !== selectedSavedFilter.id)))
    setActiveSavedFilterId(null)
    setSavedViewEditor(null)
  }

  return (
    <section className="workspace-page" aria-labelledby="workspace-heading">
      <header className="page-heading">
        <div>
          <div className="eyebrow"><span className="live-dot" /> Live workspace</div>
          <h1 id="workspace-heading">Keep execution connected to intent.</h1>
          <p>{selectedView.description}. Every change stays local to this workspace.</p>
        </div>
        <div className="page-heading__actions">
          <IconButton
            disabled={isRefreshing}
            icon="refresh"
            label="Refresh workspace"
            onClick={onRefresh}
          />
          <Button
            data-workspace-focus-fallback=""
            icon="plus"
            onClick={onCreateTask}
            variant="primary"
          >New task</Button>
        </div>
      </header>

      <div className="metrics-strip" aria-label="Workspace summary">
        <div><span>Active</span><strong>{active.length}</strong><small>{workspace.tasks.filter((task) => task.status === 'started').length} in progress</small></div>
        <div><span>Completion</span><strong>{workspace.tasks.length ? Math.round((completed / workspace.tasks.length) * 100) : 0}%</strong><small>{completed} of {workspace.tasks.length} tasks</small></div>
        <div><span>Aligned</span><strong>{aligned}</strong><small>across {workspace.objectives.length} objectives</small></div>
        <div className={blocked ? 'metric-attention' : ''}><span>Blocked</span><strong>{blocked}</strong><small>{critical} P0 active tasks</small></div>
      </div>

      {/* A zero-Task workspace that already has outcomes still shows the Graph
          catalog, so no onboarding overlay covers usable key results. The
          onboarding actions stay reachable in the banner just below. */}
      {!workspace.tasks.length && hasOutcomeCatalog ? (
        <div className="workspace-onboarding-banner">
          <p>No Tasks yet. Your outcomes are shown below.</p>
          <div className="workspace-onboarding-actions">
            <Button icon="target" onClick={onOpenObjectives} variant="primary">Define an objective</Button>
            <Button icon="plus" onClick={onCreateTask}>Create first task</Button>
          </div>
        </div>
      ) : null}
      {workspace.tasks.length || hasOutcomeCatalog ? <>
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
            onClick={() => setFiltersOpen((open) => !open)}
            ref={filterTriggerRef}
            type="button"
          >
            <span>Filters</span>
            {activeFilterCount ? <strong>{activeFilterCount}</strong> : null}
            <Icon name="chevronDown" size={14} />
          </button>
          {filtersOpen ? <div className="filter-controls" id="workspace-filter-panel">
          <label>
            <span className="sr-only">Filter by status</span>
            <select
              aria-label="Filter by status"
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
            </select>
            <Icon name="chevronDown" size={14} />
          </label>
          <label>
            <span className="sr-only">Completed task visibility</span>
            <select
              aria-label="Completed task visibility"
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
            </select>
            <Icon name="chevronDown" size={14} />
          </label>
          <label>
            <span className="sr-only">Filter by priority</span>
            <select
              aria-label="Filter by priority"
              onChange={(event) => updateUrl({ priority: event.target.value as AppUrlState['priority'] })}
              value={state.priority}
            >
              <option value="all">All priorities</option>
              {TASK_PRIORITIES.map((priority) => <option key={priority} value={priority}>{priority}</option>)}
            </select>
            <Icon name="chevronDown" size={14} />
          </label>
          <label>
            <span className="sr-only">Filter by readiness</span>
            <select
              aria-label="Filter by readiness"
              onChange={(event) => updateUrl({ readiness: event.target.value as AppUrlState['readiness'] })}
              value={state.readiness}
            >
              <option value="all">All readiness</option>
              <option value="ready">Ready to act</option>
              <option value="blocked">Blocked work</option>
            </select>
            <Icon name="chevronDown" size={14} />
          </label>
          <label>
            <span className="sr-only">Filter by due timing</span>
            <select
              aria-label="Filter by due timing"
              onChange={(event) => updateUrl({ timing: event.target.value as AppUrlState['timing'] })}
              value={state.timing}
            >
              <option value="all">All due timing</option>
              <option value="overdue">Overdue</option>
              <option value="today">Due today</option>
              <option value="soon">Due soon</option>
              <option value="unscheduled">No due date</option>
            </select>
            <Icon name="chevronDown" size={14} />
          </label>
          <label>
            <span className="sr-only">Filter by objective</span>
            <select
              aria-label="Filter by objective"
              onChange={(event) => updateUrl({ objectiveId: event.target.value })}
              value={state.objectiveId}
            >
              <option value="all">All objectives</option>
              {workspace.objectives.map((objective) => (
                <option key={objective.id} value={objective.id}>
                  {objective.id} · {getObjectiveTitle(objective)}
                </option>
              ))}
            </select>
            <Icon name="chevronDown" size={14} />
          </label>
          <label>
            <span className="sr-only">Filter by outcome</span>
            <select
              aria-label="Filter by outcome"
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
            </select>
            <Icon name="chevronDown" size={14} />
          </label>
          </div> : null}
        </div>
      </div>

      <div className="active-filter-row">
        <span>
          {`${projection.counts.visible} of ${projection.counts.canonicalTotal}`} tasks shown
          {projection.counts.hiddenCompleted
            ? ` · ${projection.counts.hiddenCompleted} completed hidden`
            : ''}
          {projection.counts.retained
            ? ` · ${projection.counts.retained} kept open`
            : ''}
          {' · '}
          {workspace.edges.length} canonical relationships
        </span>
        <span className="saved-filter-controls">
          <select
            aria-label="Saved filters"
            onChange={(event) => applySavedFilter(event.target.value)}
            value={selectedSavedFilter?.id ?? ''}
          >
            <option value="">Saved filters</option>
            {savedFilters.map((filter) => <option key={filter.id} value={filter.id}>{filter.name}</option>)}
          </select>
          <button
            className="text-button"
            disabled={Boolean(matchingSavedFilter)}
            onClick={() => setSavedViewEditor({ mode: 'create', name: suggestedSavedViewName(state) })}
            type="button"
          >Save view</button>
          {selectedSavedFilter ? (
            <details className="saved-view-menu">
              <summary aria-label="Saved view actions" role="button"><Icon name="more" size={14} /></summary>
              <div>
                <button
                  aria-label="Update saved view"
                  disabled={!selectedSavedFilterChanged}
                  onClick={updateSelectedFilter}
                  type="button"
                >Update current filters</button>
                <button
                  aria-label="Rename saved view"
                  onClick={() => setSavedViewEditor({ mode: 'rename', name: selectedSavedFilter.name })}
                  type="button"
                >Rename</button>
                <button onClick={removeSelectedFilter} type="button">Remove saved view</button>
              </div>
            </details>
          ) : null}
        </span>
        {savedViewEditor ? (
          <form
            className="saved-view-editor"
            onSubmit={(event) => { event.preventDefault(); submitSavedViewName() }}
          >
            <label>
              <span className="sr-only">Saved view name</span>
              <input
                aria-label="Saved view name"
                autoFocus
                maxLength={120}
                onChange={(event) => setSavedViewEditor({ ...savedViewEditor, name: event.target.value })}
                value={savedViewEditor.name}
              />
            </label>
            <button className="text-button" disabled={!savedViewEditor.name.trim()} type="submit">
              {savedViewEditor.mode === 'create' ? 'Create saved view' : 'Save name'}
            </button>
            <button className="text-button" onClick={() => setSavedViewEditor(null)} type="button">Cancel</button>
          </form>
        ) : null}
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
      </div>

      {/* While the Graph context modal owns the anchor its controls live inside
          that dialog, so this outside panel is suppressed to avoid a second,
          inert copy. Both render the same component from the same owner. */}
      {activeContextTargetTaskId ? null : (
        <TaskPrerequisiteContext
          anchorTaskId={prerequisiteAnchorTaskId}
          onClearReveal={() => setRevealRequest(null)}
          onOpenTask={(taskId) => updateUrl({ taskId, captureId: null })}
          onReveal={revealPrerequisites}
          projection={projection}
        />
      )}

      <div className="workspace-canvas">
        <WorkspaceViews
          edges={workspace.edges}
          notes={workspace.notes}
          objectiveId={state.objectiveId}
          objectives={workspace.objectives}
          onChangeTaskStatus={onChangeTaskStatus}
          onSelectObjective={(objectiveId) => updateUrl({
            objectiveId: state.objectiveId === objectiveId ? 'all' : objectiveId,
            taskId: null,
            captureId: null,
          })}
          onSelectTask={(taskId) => updateUrl({
            taskId: state.taskId === taskId ? null : taskId,
            captureId: null,
          })}
          priority={state.priority}
          readiness={state.readiness}
          timing={state.timing}
          today={today}
          search={state.search}
          selectedTaskId={state.taskId}
          selectedObjectiveId={state.objectiveId === 'all' ? null : state.objectiveId}
          status={state.status}
          tasks={workspace.tasks}
          projection={projection}
          keyResultProjection={keyResultProjection}
          onSelectOutcome={selectOutcome}
          outcome={outcomeFilter}
          contextTargetTaskId={activeContextTargetTaskId}
          focusPinnedTaskId={activeFocusPinnedTaskId}
          onContextTargetChange={setContextTargetTaskId}
          onFocusPinChange={setFocusPinnedTaskId}
          renderPopupPrerequisites={(taskId) => (
            <TaskPrerequisiteContext
              anchorTaskId={taskId}
              onClearReveal={() => setRevealRequest(null)}
              onOpenTask={(target) => updateUrl({ taskId: target, captureId: null })}
              onReveal={revealPrerequisites}
              projection={projection}
            />
          )}
          view={state.view}
        />
      </div>
      </> : (
        <div className="workspace-canvas workspace-canvas--first-run">
          <EmptyState
            action={(
              <div className="workspace-onboarding-actions">
                <Button icon="target" onClick={onOpenObjectives} variant="primary">Define an objective</Button>
                <Button icon="plus" onClick={onCreateTask}>Create first task</Button>
              </div>
            )}
            icon="target"
            title="Start with an outcome—or capture the first task."
          >
            Objectives describe what success looks like. Tasks carry the next concrete action and
            can be aligned to an Objective now or later. Both remain local planning facts.
          </EmptyState>
          <KeyResultCatalog
            keyResults={keyResultProjection.keyResults}
            onSelectOutcome={selectOutcome}
            selected={selectedPair}
          />
        </div>
      )}
    </section>
  )
}
