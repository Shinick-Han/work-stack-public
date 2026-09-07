import { WorkspaceViews } from 'features/workspace/views'
import { OutcomeNavigator } from './views/OutcomeNavigator'
import {
  type AppUrlState,
  type TaskStatus,
  type WorkspaceProjection,
} from '../../domain/types'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  encodeOutcomeFilter,
  normalizeOutcomeFilter,
  type OutcomeFilter,
} from './views/keyResultModel'
import { projectOutcomeHierarchy } from '../../domain/outcomeHierarchy'
import {
  filterCoordinates,
  readSavedFilters,
  sameSavedFilter,
  writeSavedFilters,
} from './savedFilters'
import { asTaskStatus } from './views/viewModels'
import {
  normalizeDoneVisibility,
  projectCompletedTaskVisibility,
  completedVisibilityScopeKey,
  type CompletedVisibilityReveal,
  type DoneVisibility,
} from './views/completedTaskVisibility'
import { TaskPrerequisiteContext } from './views/TaskPrerequisiteContext'
import { useLocalToday } from '../focus/useLocalToday'
import { ActiveFilterChips, WorkspaceCountsSummary } from './ActiveFilterChips'
import { SavedViewControls, type SavedViewEditor } from './SavedViewControls'
import { WorkspacePageHeading, WorkspaceSummaryStrip } from './WorkspaceSummary'
import { WorkspaceToolbar } from './WorkspaceToolbar'
import {
  WorkspaceFirstRunStage,
  WorkspaceOnboardingBanner,
} from './WorkspaceOnboarding'
import { suggestedSavedViewName, viewMeta } from './workspaceFilterLabels'

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

function retainBoardCompletedTaskIds(
  current: readonly string[],
  taskId: string,
  status: TaskStatus,
  view: AppUrlState['view'],
  doneVisibility: DoneVisibility | undefined,
): string[] {
  if (view !== 'board' || normalizeDoneVisibility(doneVisibility) === 'hide' || status !== 'done') {
    if (!current.includes(taskId)) return current as string[]
    return current.filter((id) => id !== taskId)
  }
  return current.includes(taskId) ? current as string[] : [...current, taskId]
}

function pruneBoardCompletedTaskIds(
  current: readonly string[],
  tasks: readonly { id: string; status?: string }[],
): string[] {
  if (!current.length) return current as string[]
  const doneIds = new Set(
    tasks.filter((item) => asTaskStatus(item.status) === 'done').map((item) => item.id),
  )
  const next = current.filter((id) => doneIds.has(id))
  if (next.length === current.length && next.every((id, index) => id === current[index])) {
    return current as string[]
  }
  return next
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
  const [sessionRetainedTaskIds, setSessionRetainedTaskIds] = useState<string[]>([])
  const sessionRetainedTaskIdsRef = useRef(sessionRetainedTaskIds)
  sessionRetainedTaskIdsRef.current = sessionRetainedTaskIds
  const boardRetentionRef = useRef({
    view: state.view,
    doneVisibility: state.doneVisibility,
  })
  boardRetentionRef.current = {
    view: state.view,
    doneVisibility: state.doneVisibility,
  }

  const commitTaskStatus = async (taskId: string, status: TaskStatus) => {
    const { view, doneVisibility } = boardRetentionRef.current
    const wasRetained = sessionRetainedTaskIdsRef.current.includes(taskId)
    setSessionRetainedTaskIds((current) => {
      const next = retainBoardCompletedTaskIds(current, taskId, status, view, doneVisibility)
      sessionRetainedTaskIdsRef.current = next
      return next
    })
    try {
      await onChangeTaskStatus(taskId, status)
      // A stale workspace/SSE refresh can prune this ID while the mutation is
      // in flight. Reassert only the eligible target after success so other
      // retained IDs stay put.
      const latest = boardRetentionRef.current
      setSessionRetainedTaskIds((current) => {
        const next = retainBoardCompletedTaskIds(
          current,
          taskId,
          status,
          latest.view,
          latest.doneVisibility,
        )
        sessionRetainedTaskIdsRef.current = next
        return next
      })
    } catch (error) {
      setSessionRetainedTaskIds((current) => {
        const next = wasRetained
          ? (current.includes(taskId) ? current : [...current, taskId])
          : current.filter((id) => id !== taskId)
        sessionRetainedTaskIdsRef.current = next
        return next
      })
      throw error
    }
  }

  useEffect(() => {
    if (normalizeDoneVisibility(state.doneVisibility) !== 'hide') return
    setSessionRetainedTaskIds((current) => (current.length ? [] : current))
  }, [state.doneVisibility])

  useEffect(() => {
    setSessionRetainedTaskIds((current) => pruneBoardCompletedTaskIds(current, workspace.tasks))
  }, [workspace.tasks])

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
    sessionRetainedTaskIds: state.view === 'board' ? sessionRetainedTaskIds : undefined,
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
    sessionRetainedTaskIds,
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
  const outcomeHierarchy = useMemo(
    () => projectOutcomeHierarchy({
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
  const keyResultProjection = outcomeHierarchy.projection
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
  const changeOutcomeFilter = (filter: OutcomeFilter) => updateUrl({
    outcomeFilter: filter,
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

  const outcomeNavigator = (
    <OutcomeNavigator
      filter={outcomeFilter}
      hierarchy={outcomeHierarchy}
      onFilterChange={changeOutcomeFilter}
    />
  )

  return (
    <section className="workspace-page" aria-labelledby="workspace-heading">
      <WorkspacePageHeading
        description={selectedView.description}
        isRefreshing={isRefreshing}
        onCreateTask={onCreateTask}
        onRefresh={onRefresh}
      />

      <WorkspaceSummaryStrip workspace={workspace} />

      {!workspace.tasks.length && hasOutcomeCatalog ? (
        <WorkspaceOnboardingBanner onCreateTask={onCreateTask} onOpenObjectives={onOpenObjectives} />
      ) : null}
      {workspace.tasks.length || hasOutcomeCatalog ? <>
      <WorkspaceToolbar
        activeFilterCount={activeFilterCount}
        filterTriggerRef={filterTriggerRef}
        filtersOpen={filtersOpen}
        objectives={workspace.objectives}
        onToggleFilters={() => setFiltersOpen((open) => !open)}
        outcomeNodes={outcomeNodes}
        outcomeValue={outcomeValue}
        state={state}
        unresolvedSelected={unresolvedSelected}
        updateUrl={updateUrl}
      />

      <div className="active-filter-row">
        <WorkspaceCountsSummary counts={projection.counts} edgeCount={workspace.edges.length} />
        <SavedViewControls
          editor={savedViewEditor}
          hasMatchingSavedFilter={Boolean(matchingSavedFilter)}
          onApply={applySavedFilter}
          onCancelEditor={() => setSavedViewEditor(null)}
          onEditorNameChange={(name) => setSavedViewEditor((current) => (
            current ? { ...current, name } : current
          ))}
          onRemove={removeSelectedFilter}
          onStartCreate={() => setSavedViewEditor({ mode: 'create', name: suggestedSavedViewName(state) })}
          onStartRename={() => setSavedViewEditor(selectedSavedFilter
            ? { mode: 'rename', name: selectedSavedFilter.name }
            : null)}
          onSubmitEditor={submitSavedViewName}
          onUpdate={updateSelectedFilter}
          savedFilters={savedFilters}
          selectedSavedFilter={selectedSavedFilter}
          selectedSavedFilterChanged={selectedSavedFilterChanged}
        />
        <ActiveFilterChips
          hasActiveFilters={hasActiveFilters}
          outcomeFilter={outcomeFilter}
          state={state}
          updateUrl={updateUrl}
        />
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

      <div className="workspace-stage">
        {outcomeNavigator}
        <div className="workspace-canvas">
        <WorkspaceViews
          workspaceId={workspace.workspace.id}
          edges={workspace.edges}
          notes={workspace.notes}
          objectiveId={state.objectiveId}
          objectives={workspace.objectives}
          onChangeTaskStatus={commitTaskStatus}
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
      </div>
      </> : (
        <WorkspaceFirstRunStage
          onCreateTask={onCreateTask}
          onOpenObjectives={onOpenObjectives}
          outcomeNavigator={outcomeNavigator}
        />
      )}
    </section>
  )
}
