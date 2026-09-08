import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../../api/client'
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
import { removeDeletedTaskFromWorkspace } from './taskDeletionProjection'
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
import {
  drawerClassName,
  TaskDrawerChrome,
  TaskDrawerDialogs,
  type DrawerSurface,
  type DrawerTab,
  type SaveState,
} from './TaskDrawerPresentation'
import { selectTaskDrawerData } from './taskDrawerSelectors'

interface TaskDrawerProps {
  taskId: string
  workspace: WorkspaceProjection
  onClose: () => void
  onNavigationLockChange?: (locked: boolean) => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onOpenObjective?: (objectiveId: string) => void
  onOpenTask?: (taskId: string) => void
  onRecordProgress?: (taskId: string) => void
  providerGates?: MicrosoftProviderGates
}

function saveRunForTask(current: SaveRun, taskId: string) {
  if (current.taskId === taskId) return current
  current.detached = true
  return createSaveRun(taskId)
}

function retryAvailable(saveState: SaveState, run: SaveRun) {
  return saveState === 'error' && hasPatch(run.queued)
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

function cacheReply(queryClient: QueryClient, taskId: string, updated: ReplyCommand) {
  queryClient.setQueryData(['task', taskId], (current: TaskDetail | undefined) => current ? {
    ...current,
    replies: [...current.replies.filter((reply) => reply.id !== updated.id), updated],
  } : current)
}

async function createTaskReply(queryClient: QueryClient, taskId: string, input: ApprovedReplyInput) {
  const created = await api.createReply(input)
  cacheReply(queryClient, taskId, created)
  await queryClient.invalidateQueries({ queryKey: ['task', taskId] })
  return created
}

async function importTaskReplyReceipt(queryClient: QueryClient, taskId: string, replyId: string, receipt: ReplyReceipt) {
  const updated = await api.importReplyReceipt(replyId, receipt)
  cacheReply(queryClient, taskId, updated)
  await queryClient.invalidateQueries({ queryKey: ['task', taskId] })
  return updated
}

function applyConfirmedPermanentDeletion(queryClient: QueryClient, taskId: string) {
  queryClient.setQueryData<WorkspaceProjection>(['workspace'], (current) => (
    current ? removeDeletedTaskFromWorkspace(current, taskId) : current
  ))
  void queryClient.cancelQueries({ queryKey: ['task', taskId] })
  queryClient.removeQueries({ queryKey: ['task', taskId] })
  void queryClient.invalidateQueries({ queryKey: ['workspace'] })
}

function scheduleDeletedTaskFocusHandoff() {
  window.setTimeout(() => {
    const fallback = document.querySelector<HTMLElement>('[data-workspace-focus-fallback]')
    if (fallback) {
      fallback.focus()
      return
    }
    document.querySelector<HTMLElement>('button[aria-current="page"]')?.focus()
  }, 0)
}

function applySavedTaskAction(
  queryClient: QueryClient,
  taskId: string,
  run: SaveRun,
  updated: Task,
  onNavigationLockChange: ((locked: boolean) => void) | undefined,
  setDraft: (task: Task) => void,
  setTagText: (value: string) => void,
) {
  run.confirmed = updated
  run.dirtyFields.clear()
  run.queued = {}
  run.inFlight = null
  run.inFlightBase = null
  run.autoRebaseUsed = false
  onNavigationLockChange?.(false)
  setDraft(updated)
  setTagText(updated.tags.join(', '))
  queryClient.setQueryData(['task', taskId], (current: TaskDetail | undefined) => current ? {
    ...current,
    task: updated,
  } : current)
  queryClient.setQueryData<WorkspaceProjection>(['workspace'], (current) => current ? {
    ...current,
    tasks: current.tasks.map((item) => item.id === updated.id ? updated : item),
  } : current)
}

function closeAfterPermanentDeletion(
  queryClient: QueryClient,
  taskId: string,
  onClose: () => void,
  setTaskRemoved: (value: boolean) => void,
  setTaskActionsOpen: (value: boolean) => void,
) {
  setTaskRemoved(true)
  applyConfirmedPermanentDeletion(queryClient, taskId)
  setTaskActionsOpen(false)
  onClose()
  scheduleDeletedTaskFocusHandoff()
}

export function TaskDrawer({
  onClose,
  onNavigationLockChange,
  onNotice,
  onOpenObjective,
  onOpenTask,
  onRecordProgress,
  providerGates = microsoftProviderGates,
  taskId,
  workspace,
}: TaskDrawerProps) {
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<DrawerTab>('resume')
  const [surface, setSurface] = useState<DrawerSurface>('task')
  const [draft, setDraft] = useState<Task | null>(null)
  const [tagText, setTagText] = useState('')
  const [replyOpen, setReplyOpen] = useState(false)
  const [snapshotOpen, setSnapshotOpen] = useState(false)
  const [taskActionsOpen, setTaskActionsOpen] = useState(false)
  const [taskRemoved, setTaskRemoved] = useState(false)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)
  const mountedRef = useRef(true)
  const savedTimerRef = useRef<number | null>(null)
  const saveRunRef = useRef<SaveRun>(createSaveRun(taskId))
  saveRunRef.current = saveRunForTask(saveRunRef.current, taskId)

  const detailQuery = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => api.getTask(taskId),
    enabled: !taskRemoved,
  })

  useEffect(() => {
    setTab('resume')
    setSurface('task')
    setReplyOpen(false)
    setSnapshotOpen(false)
    setTaskActionsOpen(false)
    setTaskRemoved(false)
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

  const createReply = async (input: ApprovedReplyInput) => {
    const created = await createTaskReply(queryClient, taskId, input)
    onNotice(`${created.id} approved; copy it to the connected agent when ready`)
    return created
  }

  const importReplyReceipt = async (replyId: string, receipt: ReplyReceipt) => {
    const updated = await importTaskReplyReceipt(queryClient, taskId, replyId, receipt)
    onNotice(`${updated.id} receipt recorded as ${updated.state}`)
    return updated
  }

  const recordTaskAction = (updated: Task) => {
    applySavedTaskAction(queryClient, taskId, saveRunRef.current, updated, onNavigationLockChange, setDraft, setTagText)
  }

  const handlePermanentlyDeleted = () => {
    closeAfterPermanentDeletion(queryClient, taskId, onClose, setTaskRemoved, setTaskActionsOpen)
  }

  const savedTask = detailQuery.data?.task ?? null
  // Stable so the subview's Escape listener subscribes once per surface, not
  // once per render of the drawer.
  const closeContext = useCallback(() => {
    setSurface('task')
    setTab('resume')
  }, [])

  return (
    <aside aria-label={`Task ${taskId}`} className={drawerClassName(surface, tab, Boolean(draft && savedTask))}>
      <TaskDrawerChrome
        activity={taskDetailActivity(detailQuery.data)}
        canRetry={retryAvailable(saveState, saveRunRef.current)}
        context={taskDetailContext(detailQuery.data)}
        draft={draft}
        loadError={taskDetailError(detailQuery.isError, detailQuery.error)}
        loadPending={detailQuery.isPending}
        markDirty={markDirty}
        navigateAfterSave={navigateAfterSave}
        navigationLocked={navigationLocked}
        onActivityToggle={() => setReplyOpen((value) => !value)}
        onClose={() => navigateAfterSave(onClose)}
        onContextBack={closeContext}
        onContextCreate={createReply}
        onContextImport={importReplyReceipt}
        onDiscard={discardUnsavedChanges}
        onDraftChange={setDraft}
        onInvalidTitle={() => {
          setSaveState('error')
          setSaveError('Task title cannot be empty. Restore a title or discard the unsaved change.')
        }}
        onMore={() => setTaskActionsOpen(true)}
        onOpenContext={() => setSurface('context')}
        onOpenObjective={onOpenObjective}
        onOpenSnapshot={() => setSnapshotOpen(true)}
        onOpenTask={onOpenTask}
        onRecordProgress={onRecordProgress ? () => navigateAfterSave(() => onRecordProgress(taskId)) : undefined}
        onRetry={() => { void retrySave() }}
        onSave={(patch) => { void save(patch) }}
        onTab={setTab}
        onTagTextChange={setTagText}
        progressLocked={navigationBlocked()}
        providerGates={providerGates}
        replies={taskDetailReplies(detailQuery.data)}
        replyOpen={replyOpen}
        saveError={saveError}
        saveState={saveState}
        savedTask={savedTask}
        selection={selection}
        surface={surface}
        tab={tab}
        tagText={tagText}
        taskId={taskId}
        workspace={workspace}
      />
      <TaskDrawerDialogs
        draft={draft}
        onActionClose={() => setTaskActionsOpen(false)}
        onNotice={onNotice}
        onPermanentlyDeleted={handlePermanentlyDeleted}
        onSaved={recordTaskAction}
        onSnapshotClose={() => setSnapshotOpen(false)}
        snapshotOpen={snapshotOpen}
        taskActionsOpen={taskActionsOpen}
        taskId={taskId}
        workspaceUid={workspace.workspace.id}
      />
    </aside>
  )
}
