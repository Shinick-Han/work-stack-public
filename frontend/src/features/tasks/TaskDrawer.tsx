import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../../api/client'
import { Icon } from '../../components/Icon'
import { EmptyState, IconButton, LoadingBlock } from '../../components/Primitives'
import {
  microsoftProviderGates,
  type MicrosoftProviderGates,
} from '../../config/providerGates'
import {
  type ApprovedReplyInput,
  type ReplyCommand,
  type ReplyReceipt,
  type Task,
  type TaskDetail,
  type WorkspaceProjection,
} from '../../domain/types'
import { getErrorMessage } from '../../utils/format'
import { SnapshotExportDialog } from './SnapshotExportDialog'
import { TaskActionsDialog } from './TaskActionsDialog'
import {
  createSaveRun,
  hasPatch,
  overlayDirtyFields,
  patchFields,
  pruneServerEqualFields,
  sameValue,
  type EditableTaskField,
  type EditableTaskPatch,
  type SaveRun,
} from './taskDrawerModel'
import { TaskActivityTimeline, TaskContextTimeline } from './TaskDrawerTimelines'
import { TaskRelationshipsSection } from './TaskRelationshipsSection'
import { TaskOverviewEditor } from './TaskOverviewEditor'
import { TaskOverviewSummary } from './TaskOverviewSummary'
import { TaskReplySection } from './TaskReplySection'
import { selectTaskDrawerData, type TaskDrawerSelection } from './taskDrawerSelectors'

type DrawerTab = 'overview' | 'context' | 'activity'
type SaveState = 'idle' | 'saving' | 'saved' | 'error'

interface TaskDrawerProps {
  taskId: string
  workspace: WorkspaceProjection
  onClose: () => void
  onNavigationLockChange?: (locked: boolean) => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onOpenObjective?: (objectiveId: string) => void
  onOpenTask?: (taskId: string) => void
  providerGates?: MicrosoftProviderGates
}

function saveRunForTask(current: SaveRun, taskId: string) {
  if (current.taskId === taskId) return current
  current.detached = true
  return createSaveRun(taskId)
}

function draftContextCount(draft: Task | null) {
  return draft ? draft.context_count : 0
}

function retryAvailable(saveState: SaveState, run: SaveRun) {
  return saveState === 'error' && hasPatch(run.queued)
}

function draftTabActive(tab: DrawerTab, expected: DrawerTab, draft: Task | null) {
  return tab === expected && Boolean(draft)
}

function taskDetailError(isError: boolean, error: unknown) {
  return isError ? error : null
}

function taskDetailContext(detail?: TaskDetail) {
  return detail ? detail.context : []
}

function taskDetailActivity(detail?: TaskDetail) {
  return detail ? detail.activity : []
}

function taskDetailReplies(detail?: TaskDetail) {
  return detail ? detail.replies : []
}

function saveStateLabel(saveState: SaveState) {
  if (saveState === 'saving') return 'Saving…'
  if (saveState === 'saved') return 'Saved'
  if (saveState === 'error') return 'Not saved'
  return ''
}

function TaskDrawerHeader({ navigationLocked, onClose, onMore, saveState, taskId }: {
  navigationLocked: boolean
  onClose: () => void
  onMore: () => void
  saveState: SaveState
  taskId: string
}) {
  return <header className="drawer-header">
    <div className="drawer-header__identity"><span className="task-glyph"><Icon name="task" size={17} /></span><div><span>Task</span><strong>{taskId}</strong></div></div>
    <div className="drawer-header__actions">
      <span aria-live="polite" className={`save-state save-state--${saveState}`}>{saveStateLabel(saveState)}</span>
      <IconButton disabled={navigationLocked} icon="more" label="More task actions" onClick={onMore} variant="ghost" />
      <IconButton disabled={navigationLocked} icon="close" label="Close task drawer" onClick={onClose} variant="ghost" />
    </div>
  </header>
}

function TaskDrawerTabs({ contextCount, onTab, tab }: { contextCount: number; onTab: (tab: DrawerTab) => void; tab: DrawerTab }) {
  return <nav aria-label="Task details" className="drawer-tabs">
    {(['overview', 'context', 'activity'] as const).map((item) => (
      <button aria-selected={tab === item} className={tab === item ? 'is-active' : ''} key={item} onClick={() => onTab(item)} role="tab" type="button">
        {item[0].toUpperCase() + item.slice(1)}
        {item === 'context' && contextCount ? <span>{contextCount}</span> : null}
      </button>
    ))}
  </nav>
}

function TaskDrawerLoadState({ draft, error, pending }: { draft: Task | null; error: unknown; pending: boolean }) {
  if (pending) return <LoadingBlock label="Loading task details…" />
  if (error) return <EmptyState icon="warning" title="Task detail unavailable">{getErrorMessage(error)}</EmptyState>
  if (!draft) return <EmptyState icon="warning" title="Task detail unavailable">The task did not include a readable projection.</EmptyState>
  return null
}

function TaskDrawerOverviewTab({ active, canRetry, draft, markDirty, navigateAfterSave, navigationLocked, onDiscard, onDraftChange, onInvalidTitle, onOpenObjective, onOpenSnapshot, onOpenTask, onRetry, onSave, onTagTextChange, saveError, saveState, selection, tagText, workspace }: {
  active: boolean
  canRetry: boolean
  draft: Task | null
  markDirty: (field: EditableTaskField) => void
  navigateAfterSave: (action: () => void) => void
  navigationLocked: boolean
  onDiscard: () => void
  onDraftChange: (task: Task) => void
  onInvalidTitle: () => void
  onOpenObjective?: (objectiveId: string) => void
  onOpenSnapshot: () => void
  onOpenTask?: (taskId: string) => void
  onRetry: () => void
  onSave: (patch: EditableTaskPatch) => void
  onTagTextChange: (value: string) => void
  saveError: string | null
  saveState: SaveState
  selection: TaskDrawerSelection
  tagText: string
  workspace: WorkspaceProjection
}) {
  if (!active || !draft) return null
  const openObjective = onOpenObjective ? (objectiveId: string) => navigateAfterSave(() => onOpenObjective(objectiveId)) : undefined
  const relationshipSection = onOpenTask ? (
    <TaskRelationshipsSection
      childTasks={selection.childTasks}
      dependentTasks={selection.dependentTasks}
      dependencyTasks={selection.dependencyTasks}
      disabled={navigationLocked}
      onOpenTask={(relatedTaskId) => navigateAfterSave(() => onOpenTask(relatedTaskId))}
      parentTask={selection.parentTask}
    />
  ) : null
  return <div className="drawer-overview">
    <TaskOverviewSummary
      canDiscard={saveState === 'error'}
      canRetry={canRetry}
      draft={draft}
      isSaving={saveState === 'saving'}
      navigationLocked={navigationLocked}
      objectives={selection.taskObjectives}
      onDiscard={onDiscard}
      onDraftChange={onDraftChange}
      onInvalidTitle={onInvalidTitle}
      onMarkDirty={markDirty}
      onOpenObjective={openObjective}
      onOpenSnapshot={onOpenSnapshot}
      onRetry={onRetry}
      onSaveTitle={(title) => onSave({ title })}
      saveError={saveError}
    />
    <TaskOverviewEditor
      availableDependencyTasks={selection.availableDependencyTasks}
      availableParentTasks={selection.availableParentTasks}
      draft={draft}
      isSaving={saveState === 'saving'}
      onDraftChange={onDraftChange}
      onMarkDirty={markDirty}
      onSave={onSave}
      onTagTextChange={onTagTextChange}
      relationshipSection={relationshipSection}
      tagText={tagText}
      workspace={workspace}
    />
  </div>
}

function TaskDrawerContextTab({ active, context, onCreate, onImportReceipt, onToggle, open, providerGates, replies, selection, taskId }: {
  active: boolean
  context: TaskDetail['context']
  onCreate: (input: ApprovedReplyInput) => Promise<ReplyCommand>
  onImportReceipt: (replyId: string, receipt: ReplyReceipt) => Promise<ReplyCommand>
  onToggle: () => void
  open: boolean
  providerGates: MicrosoftProviderGates
  replies: ReplyCommand[]
  selection: TaskDrawerSelection
  taskId: string
}) {
  if (!active) return null
  return <div className="context-tab">
    <TaskReplySection
      onCreate={onCreate}
      onImportReceipt={onImportReceipt}
      onToggle={onToggle}
      open={open}
      replies={replies}
      sources={selection.replySources}
      taskId={taskId}
      unavailableSources={selection.replyUnavailableSources}
    />
    <TaskContextTimeline context={context} providerGates={providerGates} />
  </div>
}

function TaskDrawerActivityTab({ active, activity }: { active: boolean; activity: TaskDetail['activity'] }) {
  return active ? <TaskActivityTimeline activity={activity} /> : null
}

function TaskDrawerDialogs({ draft, onActionClose, onDeleted, onNotice, onSaved, onSnapshotClose, snapshotOpen, taskActionsOpen, taskId }: {
  draft: Task | null
  onActionClose: () => void
  onDeleted: () => void
  onNotice: TaskDrawerProps['onNotice']
  onSaved: (task: Task) => void
  onSnapshotClose: () => void
  snapshotOpen: boolean
  taskActionsOpen: boolean
  taskId: string
}) {
  return <>
    <SnapshotExportDialog onClose={onSnapshotClose} onNotice={onNotice} open={snapshotOpen} taskId={taskId} />
    {draft ? <TaskActionsDialog onClose={onActionClose} onDeleted={onDeleted} onNotice={onNotice} onSaved={onSaved} open={taskActionsOpen} task={draft} /> : null}
  </>
}

export function TaskDrawer({
  onClose,
  onNavigationLockChange,
  onNotice,
  onOpenObjective,
  onOpenTask,
  providerGates = microsoftProviderGates,
  taskId,
  workspace,
}: TaskDrawerProps) {
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<DrawerTab>('overview')
  const [draft, setDraft] = useState<Task | null>(null)
  const [tagText, setTagText] = useState('')
  const [replyOpen, setReplyOpen] = useState(false)
  const [snapshotOpen, setSnapshotOpen] = useState(false)
  const [taskActionsOpen, setTaskActionsOpen] = useState(false)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)
  const mountedRef = useRef(true)
  const savedTimerRef = useRef<number | null>(null)
  const saveRunRef = useRef<SaveRun>(createSaveRun(taskId))
  saveRunRef.current = saveRunForTask(saveRunRef.current, taskId)

  const detailQuery = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => api.getTask(taskId),
  })

  useEffect(() => {
    setTab('overview')
    setReplyOpen(false)
    setSnapshotOpen(false)
    setTaskActionsOpen(false)
    setDraft(null)
    setTagText('')
    setSaveState('idle')
    setSaveError(null)
    onNavigationLockChange?.(false)
    if (savedTimerRef.current !== null) window.clearTimeout(savedTimerRef.current)
    savedTimerRef.current = null
  }, [taskId])

  useEffect(() => {
    const mountedRun = saveRunRef.current
    mountedRef.current = true
    mountedRun.detached = false
    return () => {
      mountedRef.current = false
      mountedRun.detached = true
      onNavigationLockChange?.(false)
      if (savedTimerRef.current !== null) window.clearTimeout(savedTimerRef.current)
    }
  }, [])

  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      const run = saveRunRef.current
      if (!(run.running || run.blocked || run.inFlight || hasPatch(run.queued) || run.dirtyFields.size)) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', handleBeforeUnload)
    return () => window.removeEventListener('beforeunload', handleBeforeUnload)
  }, [])

  useEffect(() => {
    const incoming = detailQuery.data?.task
    const run = saveRunRef.current
    if (!incoming || incoming.id !== taskId || run.taskId !== taskId) return
    if (run.confirmed && incoming.revision < run.confirmed.revision) {
      updateCaches(run.confirmed, run.taskId)
      return
    }
    run.confirmed = incoming
    hydrateDraft(incoming, run.dirtyFields)
  }, [detailQuery.data?.task, taskId])

  const isActiveRun = (run: SaveRun) => mountedRef.current && !run.detached && saveRunRef.current === run

  const hydrateDraft = (task: Task, dirtyFields = new Set<EditableTaskField>()) => {
    if (!mountedRef.current) return
    setDraft((current) => current ? overlayDirtyFields(task, current, dirtyFields) : task)
    if (!dirtyFields.has('tags')) setTagText(task.tags.join(', '))
  }

  const updateCaches = (updated: Task, cacheTaskId = updated.id) => {
    queryClient.setQueryData<TaskDetail>(['task', cacheTaskId], (current) => {
      if (!current || current.task.revision > updated.revision) return current
      return { ...current, task: updated }
    })
    queryClient.setQueryData<WorkspaceProjection>(['workspace'], (current) => current ? {
      ...current,
      tasks: current.tasks.map((task) => task.id === updated.id && task.revision <= updated.revision ? updated : task),
    } : current)
  }

  const newestCachedTask = (run: SaveRun) => {
    let newest = run.confirmed
    const detailTask = queryClient.getQueryData<TaskDetail>(['task', run.taskId])?.task
    const workspaceTask = queryClient.getQueryData<WorkspaceProjection>(['workspace'])?.tasks.find((task) => task.id === run.taskId)
    for (const candidate of [detailTask, workspaceTask]) {
      if (candidate && (!newest || candidate.revision > newest.revision)) newest = candidate
    }
    return newest
  }

  const clearSavedTimer = () => {
    if (savedTimerRef.current !== null) window.clearTimeout(savedTimerRef.current)
    savedTimerRef.current = null
  }

  const markDirty = (field: EditableTaskField) => {
    const run = saveRunRef.current
    run.dirtyFields.add(field)
    onNavigationLockChange?.(true)
    clearSavedTimer()
    if (!run.running && !run.blocked) setSaveState('idle')
  }

  const clearPrunedDirtyFields = (run: SaveRun, beforePrune: EditableTaskPatch) => {
    for (const field of patchFields(beforePrune)) {
      if (!(field in run.queued)) run.dirtyFields.delete(field)
    }
  }

  const refreshFailedRun = async (run: SaveRun) => {
    const result = await detailQuery.refetch()
    const latest = result.data?.task
    if (latest && (!run.confirmed || latest.revision >= run.confirmed.revision)) run.confirmed = latest
    const newest = newestCachedTask(run)
    if (newest) {
      run.confirmed = newest
      updateCaches(newest, run.taskId)
      hydrateDraft(newest, run.dirtyFields)
    }
    return newest
  }

  const tryAutomaticRebase = (
    run: SaveRun,
    error: unknown,
    failedBase: Task | null,
    newest: Task | null | undefined,
  ) => {
    if (!(error instanceof ApiError) || error.code !== 'revision_conflict' || !failedBase || !newest || run.autoRebaseUsed) {
      return false
    }
    const beforePrune = run.queued
    run.queued = pruneServerEqualFields(run.queued, newest)
    clearPrunedDirtyFields(run, beforePrune)
    const overlappingFields = patchFields(run.queued).filter((field) => !sameValue(failedBase[field], newest[field]))
    if (overlappingFields.length) return false
    run.autoRebaseUsed = true
    run.blocked = false
    setSaveError(null)
    if (hasPatch(run.queued)) {
      setSaveState('saving')
    } else {
      hydrateDraft(newest)
      setSaveState('saved')
    }
    return true
  }

  const reportSaveFailure = async (run: SaveRun, error: unknown) => {
    const failed = run.inFlight ?? {}
    const failedBase = run.inFlightBase
    run.queued = { ...failed, ...run.queued }
    run.inFlight = null
    run.inFlightBase = null
    run.blocked = true
    onNavigationLockChange?.(true)
    if (!isActiveRun(run)) return

    const message = error instanceof ApiError && error.code === 'revision_conflict'
      ? 'This task changed elsewhere. Latest values were reloaded; review and try again.'
      : getErrorMessage(error)
    const newest = await refreshFailedRun(run)
    if (!isActiveRun(run)) return
    if (tryAutomaticRebase(run, error, failedBase, newest)) return
    setSaveState('error')
    setSaveError(message)
    onNotice(getErrorMessage(error), 'error')
  }

  const finalizeRun = async (run: SaveRun) => {
    await Promise.allSettled([
      queryClient.invalidateQueries({ queryKey: ['task', run.taskId] }),
      queryClient.invalidateQueries({ queryKey: ['workspace'] }),
    ])
    const newest = newestCachedTask(run)
    if (newest) {
      run.confirmed = newest
      updateCaches(newest, run.taskId)
    }
    if (!isActiveRun(run) || hasPatch(run.queued)) return false
    if (run.dirtyFields.size) {
      onNavigationLockChange?.(true)
      if (newest) hydrateDraft(newest, run.dirtyFields)
      setSaveError(null)
      setSaveState('idle')
      return true
    }
    if (newest) hydrateDraft(newest)
    run.autoRebaseUsed = false
    setSaveError(null)
    setSaveState('saved')
    onNavigationLockChange?.(false)
    clearSavedTimer()
    savedTimerRef.current = window.setTimeout(() => {
      if (isActiveRun(run)) setSaveState((value) => value === 'saved' ? 'idle' : value)
    }, 1800)
    return true
  }

  const acceptPatchResult = (run: SaveRun, base: Task, patch: EditableTaskPatch, updated: Task) => {
    const confirmed = base.revision > updated.revision ? base : updated
    run.confirmed = confirmed
    run.inFlight = null
    run.inFlightBase = null
    updateCaches(updated, run.taskId)
    for (const field of patchFields(patch)) {
      if (!(field in run.queued)) run.dirtyFields.delete(field)
    }
    const beforePrune = run.queued
    run.queued = pruneServerEqualFields(run.queued, confirmed)
    clearPrunedDirtyFields(run, beforePrune)
  }

  const saveNextQueuedPatch = async (run: SaveRun): Promise<'empty' | 'saved' | 'stop'> => {
    if (!hasPatch(run.queued)) return 'empty'
    const base = run.confirmed
    if (!base) return 'stop'
    const patch = run.queued
    run.queued = {}
    run.inFlight = patch
    run.inFlightBase = base
    let updated: Task
    try {
      updated = await api.patchTask(run.taskId, { ...patch, revision: base.revision })
    } catch (error) {
      await reportSaveFailure(run, error)
      return 'stop'
    }
    const newerCached = newestCachedTask(run)
    if (newerCached && newerCached.revision > updated.revision) {
      run.confirmed = newerCached
      updateCaches(updated, run.taskId)
      await reportSaveFailure(run, new ApiError(
        409,
        'revision_conflict',
        'This task changed again while your save response was in flight.',
      ))
      return 'stop'
    }
    acceptPatchResult(run, base, patch, updated)
    return isActiveRun(run) ? 'saved' : 'stop'
  }

  const drain = async (run: SaveRun) => {
    if (run.running || run.blocked || !run.confirmed) return
    run.running = true
    onNavigationLockChange?.(true)
    if (isActiveRun(run)) {
      clearSavedTimer()
      setSaveState('saving')
      setSaveError(null)
    }
    try {
      for (;;) {
        const outcome = await saveNextQueuedPatch(run)
        if (outcome === 'saved') continue
        if (outcome === 'stop') return
        if (await finalizeRun(run)) return
        if (!isActiveRun(run)) return
      }
    } finally {
      run.running = false
      if (isActiveRun(run) && !run.blocked && hasPatch(run.queued)) void drain(run)
    }
  }

  const pendingPatch = (run: SaveRun, patch: EditableTaskPatch) => {
    let pending = pruneServerEqualFields(patch, run.confirmed!)
    if (!run.inFlight) return pending
    for (const field of patchFields(patch)) {
      if (field in run.inFlight && !(field in pending)) pending = { ...pending, [field]: patch[field] }
    }
    return pending
  }

  const mergePendingPatch = (run: SaveRun, patch: EditableTaskPatch, pending: EditableTaskPatch) => {
    for (const field of patchFields(patch)) {
      if (field in pending) {
        run.dirtyFields.add(field)
      } else {
        delete run.queued[field]
        run.dirtyFields.delete(field)
      }
    }
    run.queued = { ...run.queued, ...pending }
  }

  const resumeSaveRun = (run: SaveRun) => {
    onNavigationLockChange?.(run.dirtyFields.size > 0 || hasPatch(run.queued) || run.running || run.blocked)
    clearSavedTimer()
    if (run.blocked && !hasPatch(run.queued)) {
      run.blocked = false
      void drain(run)
      return
    }
    if (!run.blocked && !run.running) void drain(run)
  }

  const save = (patch: EditableTaskPatch) => {
    const run = saveRunRef.current
    if (!run.confirmed || !hasPatch(patch)) return
    if (!run.running && !run.blocked) run.autoRebaseUsed = false
    mergePendingPatch(run, patch, pendingPatch(run, patch))
    resumeSaveRun(run)
  }

  const reportRetryReloadFailure = (run: SaveRun, error: unknown) => {
    run.running = false
    if (!isActiveRun(run)) return
    const message = getErrorMessage(error ?? new Error('Latest task could not be reloaded.'))
    setSaveState('error')
    setSaveError(message)
    onNotice(message, 'error')
  }

  const prepareRetry = (run: SaveRun, reloaded: Task) => {
    if (!run.confirmed || reloaded.revision >= run.confirmed.revision) run.confirmed = reloaded
    const newest = newestCachedTask(run)
    if (newest) run.confirmed = newest
    const beforePrune = run.queued
    run.queued = run.confirmed ? pruneServerEqualFields(run.queued, run.confirmed) : run.queued
    clearPrunedDirtyFields(run, beforePrune)
    if (!isActiveRun(run)) {
      run.running = false
      return false
    }
    run.autoRebaseUsed = false
    run.blocked = false
    run.running = false
    return true
  }

  const retrySave = async () => {
    const run = saveRunRef.current
    if (!run.blocked || run.running) return
    run.running = true
    onNavigationLockChange?.(true)
    clearSavedTimer()
    setSaveState('saving')
    setSaveError(null)
    const result = await detailQuery.refetch()
    if (result.error || !result.data?.task) {
      reportRetryReloadFailure(run, result.error)
      return
    }
    if (!prepareRetry(run, result.data.task)) return
    void drain(run)
  }

  const navigationBlocked = () => {
    const run = saveRunRef.current
    return run.running || run.blocked || Boolean(run.inFlight) || hasPatch(run.queued) || run.dirtyFields.size > 0
  }

  const navigateAfterSave = (action: () => void) => {
    if (navigationBlocked()) {
      onNotice('Finish saving or discard the unsaved Task changes before leaving.', 'error')
      return
    }
    action()
  }

  const discardUnsavedChanges = () => {
    const run = saveRunRef.current
    if (run.running || !run.confirmed) return
    run.queued = {}
    run.inFlight = null
    run.inFlightBase = null
    run.dirtyFields.clear()
    run.blocked = false
    run.autoRebaseUsed = false
    hydrateDraft(run.confirmed)
    setSaveError(null)
    setSaveState('idle')
    onNavigationLockChange?.(false)
    onNotice('Unsaved Task changes discarded')
  }

  const navigationLocked = saveState === 'saving' || saveState === 'error'

  const selection = useMemo(() => selectTaskDrawerData({
    context: detailQuery.data?.context ?? [],
    draft,
    providerGates,
    workspace,
  }), [detailQuery.data?.context, draft, providerGates, workspace])

  const recordReply = (updated: ReplyCommand, message: string) => {
    queryClient.setQueryData(['task', taskId], (current: typeof detailQuery.data) => current ? {
      ...current,
      replies: [...current.replies.filter((reply) => reply.id !== updated.id), updated],
    } : current)
    onNotice(message)
  }

  const createReply = async (input: ApprovedReplyInput) => {
    const created = await api.createReply(input)
    recordReply(created, `${created.id} approved; copy it to the connected agent when ready`)
    await queryClient.invalidateQueries({ queryKey: ['task', taskId] })
    return created
  }

  const importReplyReceipt = async (replyId: string, receipt: ReplyReceipt) => {
    const updated = await api.importReplyReceipt(replyId, receipt)
    recordReply(updated, `${updated.id} receipt recorded as ${updated.state}`)
    await queryClient.invalidateQueries({ queryKey: ['task', taskId] })
    return updated
  }

  const recordTaskAction = (updated: Task) => {
    saveRunRef.current.confirmed = updated
    saveRunRef.current.dirtyFields.clear()
    saveRunRef.current.queued = {}
    saveRunRef.current.inFlight = null
    saveRunRef.current.inFlightBase = null
    saveRunRef.current.autoRebaseUsed = false
    onNavigationLockChange?.(false)
    setDraft(updated)
    setTagText(updated.tags.join(', '))
    queryClient.setQueryData(['task', taskId], (current: typeof detailQuery.data) => current ? {
      ...current,
      task: updated,
    } : current)
    queryClient.setQueryData<WorkspaceProjection>(['workspace'], (current) => current ? {
      ...current,
      tasks: current.tasks.map((task) => task.id === updated.id ? updated : task),
    } : current)
  }

  return (
    <aside aria-label={`Task ${taskId}`} className="detail-drawer">
      <TaskDrawerHeader navigationLocked={navigationLocked} onClose={() => navigateAfterSave(onClose)} onMore={() => setTaskActionsOpen(true)} saveState={saveState} taskId={taskId} />
      <TaskDrawerTabs contextCount={draftContextCount(draft)} onTab={setTab} tab={tab} />
      <div className="drawer-body">
        <TaskDrawerLoadState draft={draft} error={taskDetailError(detailQuery.isError, detailQuery.error)} pending={detailQuery.isPending} />
        <TaskDrawerOverviewTab
          active={tab === 'overview'}
          canRetry={retryAvailable(saveState, saveRunRef.current)}
          draft={draft}
          markDirty={markDirty}
          navigateAfterSave={navigateAfterSave}
          navigationLocked={navigationLocked}
          onDiscard={discardUnsavedChanges}
          onDraftChange={setDraft}
          onInvalidTitle={() => {
            setSaveState('error')
            setSaveError('Task title cannot be empty. Restore a title or discard the unsaved change.')
          }}
          onOpenObjective={onOpenObjective}
          onOpenSnapshot={() => setSnapshotOpen(true)}
          onOpenTask={onOpenTask}
          onRetry={() => { void retrySave() }}
          onSave={(patch) => { void save(patch) }}
          onTagTextChange={setTagText}
          saveError={saveError}
          saveState={saveState}
          selection={selection}
          tagText={tagText}
          workspace={workspace}
        />
        <TaskDrawerContextTab active={draftTabActive(tab, 'context', draft)} context={taskDetailContext(detailQuery.data)} onCreate={createReply} onImportReceipt={importReplyReceipt} onToggle={() => setReplyOpen((value) => !value)} open={replyOpen} providerGates={providerGates} replies={taskDetailReplies(detailQuery.data)} selection={selection} taskId={taskId} />
        <TaskDrawerActivityTab active={draftTabActive(tab, 'activity', draft)} activity={taskDetailActivity(detailQuery.data)} />
      </div>
      <TaskDrawerDialogs draft={draft} onActionClose={() => setTaskActionsOpen(false)} onDeleted={onClose} onNotice={onNotice} onSaved={recordTaskAction} onSnapshotClose={() => setSnapshotOpen(false)} snapshotOpen={snapshotOpen} taskActionsOpen={taskActionsOpen} taskId={taskId} />
    </aside>
  )
}
