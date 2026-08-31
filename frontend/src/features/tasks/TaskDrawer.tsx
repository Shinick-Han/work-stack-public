import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api } from '../../api/client'
import { Icon } from '../../components/Icon'
import { Button, EmptyState, IconButton, LoadingBlock, Pill } from '../../components/Primitives'
import {
  microsoftProviderGates,
  providerReplyVerified,
  type MicrosoftProviderGates,
} from '../../config/providerGates'
import {
  MICROSOFT_PROVIDERS,
  TASK_PRIORITIES,
  TASK_STATUSES,
  type ContextItem,
  type MicrosoftProvider,
  type ReplyCommand,
  type Task,
  type TaskDetail,
  type TaskPatch,
  type WorkspaceProjection,
} from '../../domain/types'
import { formatDateTime, getErrorMessage, getObjectiveTitle, priorityLabels, safeExternalUrl, statusLabels } from '../../utils/format'
import { ReplyComposer, type ReplySource } from './ReplyComposer'
import { SnapshotExportDialog } from './SnapshotExportDialog'
import { TaskActionsDialog } from './TaskActionsDialog'
import { cyclicRelationshipCandidates } from './taskRelationships'

type DrawerTab = 'overview' | 'context' | 'activity'
type EditableTaskPatch = Omit<TaskPatch, 'revision'>
type EditableTaskField = keyof EditableTaskPatch
type SaveState = 'idle' | 'saving' | 'saved' | 'error'

interface SaveRun {
  taskId: string
  confirmed: Task | null
  queued: EditableTaskPatch
  inFlight: EditableTaskPatch | null
  inFlightBase: Task | null
  dirtyFields: Set<EditableTaskField>
  running: boolean
  blocked: boolean
  autoRebaseUsed: boolean
  detached: boolean
}

function createSaveRun(taskId: string): SaveRun {
  return {
    taskId,
    confirmed: null,
    queued: {},
    inFlight: null,
    inFlightBase: null,
    dirtyFields: new Set(),
    running: false,
    blocked: false,
    autoRebaseUsed: false,
    detached: false,
  }
}

function patchFields(patch: EditableTaskPatch): EditableTaskField[] {
  return Object.keys(patch) as EditableTaskField[]
}

function hasPatch(patch: EditableTaskPatch) {
  return patchFields(patch).length > 0
}

function sameValue(left: unknown, right: unknown) {
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((value, index) => value === right[index])
  }
  return left === right
}

function pruneServerEqualFields(patch: EditableTaskPatch, task: Task) {
  const pruned = { ...patch }
  for (const field of patchFields(pruned)) {
    if (sameValue(pruned[field], task[field])) delete pruned[field]
  }
  return pruned
}

function overlayDirtyFields(task: Task, draft: Task, fields: Set<EditableTaskField>) {
  let overlaid = { ...task }
  for (const field of fields) overlaid = { ...overlaid, [field]: draft[field] }
  return overlaid
}

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

function externalContext(item: ContextItem) {
  return Boolean(item.source || item.normalized || item.kind === 'capture' || item.type === 'capture')
}

function contextTitle(item: ContextItem) {
  return item.source?.display_title ?? item.normalized?.summary ?? item.text ?? 'Context item'
}

function activityTitle(item: TaskDetail['activity'][number]) {
  if (item.type !== 'task.planning_status' || !item.status) {
    return item.message ?? item.action ?? item.type ?? 'Task updated'
  }
  if (item.prior_status) return `${statusLabels[item.prior_status]} → ${statusLabels[item.status]}`
  return `Status recorded as ${statusLabels[item.status]}`
}

function microsoftReplySource(item: ContextItem): ReplySource | null {
  const source = item.source
  if (
    !item.id
    || !source
    || !MICROSOFT_PROVIDERS.includes(source.provider as MicrosoftProvider)
    || !source.resource_type
    || !source.connection_ref
    || !source.container_ref
    || !source.display_title
    || !source.object_ref
    || !source.version_ref
  ) return null
  return {
    capture_id: item.id,
    provider: source.provider as MicrosoftProvider,
    resource_type: source.resource_type,
    connection_ref: source.connection_ref,
    container_ref: source.container_ref,
    display_title: source.display_title,
    object_ref: source.object_ref,
    version_ref: source.version_ref,
  }
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

  if (saveRunRef.current.taskId !== taskId) {
    saveRunRef.current.detached = true
    saveRunRef.current = createSaveRun(taskId)
  }

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
    const result = await detailQuery.refetch()
    const latest = result.data?.task
    if (latest && (!run.confirmed || latest.revision >= run.confirmed.revision)) run.confirmed = latest
    const newest = newestCachedTask(run)
    if (newest) {
      run.confirmed = newest
      updateCaches(newest, run.taskId)
      hydrateDraft(newest, run.dirtyFields)
    }
    if (!isActiveRun(run)) return
    if (error instanceof ApiError && error.code === 'revision_conflict' && failedBase && newest && !run.autoRebaseUsed) {
      const beforePrune = run.queued
      run.queued = pruneServerEqualFields(run.queued, newest)
      for (const field of patchFields(beforePrune)) {
        if (!(field in run.queued)) run.dirtyFields.delete(field)
      }
      const overlappingFields = patchFields(run.queued).filter((field) => !sameValue(failedBase[field], newest[field]))
      if (!overlappingFields.length) {
        run.autoRebaseUsed = true
        run.blocked = false
        setSaveError(null)
        if (hasPatch(run.queued)) {
          setSaveState('saving')
        } else {
          hydrateDraft(newest)
          setSaveState('saved')
        }
        return
      }
    }
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
        while (hasPatch(run.queued)) {
          const base = run.confirmed
          if (!base) return
          const patch = run.queued
          run.queued = {}
          run.inFlight = patch
          run.inFlightBase = base
          let updated: Task
          try {
            updated = await api.patchTask(run.taskId, { ...patch, revision: base.revision })
          } catch (error) {
            await reportSaveFailure(run, error)
            return
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
            return
          }

          const confirmed: Task = base.revision > updated.revision ? base : updated
          run.confirmed = confirmed
          run.inFlight = null
          run.inFlightBase = null
          updateCaches(updated, run.taskId)

          for (const field of patchFields(patch)) {
            if (!(field in run.queued)) run.dirtyFields.delete(field)
          }
          const beforePrune = run.queued
          run.queued = pruneServerEqualFields(run.queued, confirmed)
          for (const field of patchFields(beforePrune)) {
            if (!(field in run.queued)) run.dirtyFields.delete(field)
          }
          if (!isActiveRun(run)) return
        }

        if (await finalizeRun(run)) return
        if (!isActiveRun(run)) return
      }
    } finally {
      run.running = false
      if (isActiveRun(run) && !run.blocked && hasPatch(run.queued)) void drain(run)
    }
  }

  const save = (patch: EditableTaskPatch) => {
    const run = saveRunRef.current
    if (!run.confirmed || !hasPatch(patch)) return
    if (!run.running && !run.blocked) run.autoRebaseUsed = false
    let pending = pruneServerEqualFields(patch, run.confirmed)
    if (run.inFlight) {
      for (const field of patchFields(patch)) {
        if (field in run.inFlight && !(field in pending)) pending = { ...pending, [field]: patch[field] }
      }
    }
    for (const field of patchFields(patch)) {
      if (field in pending) {
        run.dirtyFields.add(field)
      } else {
        delete run.queued[field]
        run.dirtyFields.delete(field)
      }
    }
    run.queued = { ...run.queued, ...pending }
    onNavigationLockChange?.(run.dirtyFields.size > 0 || hasPatch(run.queued) || run.running || run.blocked)
    clearSavedTimer()
    if (run.blocked && !hasPatch(run.queued)) {
      run.blocked = false
      void drain(run)
      return
    }
    if (!run.blocked && !run.running) void drain(run)
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
      run.running = false
      if (isActiveRun(run)) {
        const message = getErrorMessage(result.error ?? new Error('Latest task could not be reloaded.'))
        setSaveState('error')
        setSaveError(message)
        onNotice(message, 'error')
      }
      return
    }
    if (!run.confirmed || result.data.task.revision >= run.confirmed.revision) run.confirmed = result.data.task
    const newest = newestCachedTask(run)
    if (newest) run.confirmed = newest
    const beforePrune = run.queued
    run.queued = run.confirmed ? pruneServerEqualFields(run.queued, run.confirmed) : run.queued
    for (const field of patchFields(beforePrune)) {
      if (!(field in run.queued)) run.dirtyFields.delete(field)
    }
    if (!isActiveRun(run)) {
      run.running = false
      return
    }
    run.autoRebaseUsed = false
    run.blocked = false
    run.running = false
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

  const taskObjectives = useMemo(
    () => workspace.objectives.filter((objective) => draft?.objective_ids.includes(objective.id)),
    [draft?.objective_ids, workspace.objectives],
  )
  const parentTask = useMemo(
    () => workspace.tasks.find((task) => task.id === draft?.parent_id) ?? null,
    [draft?.parent_id, workspace.tasks],
  )
  const dependencyTasks = useMemo(
    () => workspace.tasks.filter((task) => draft?.dependencies.includes(task.id)),
    [draft?.dependencies, workspace.tasks],
  )
  const childTasks = useMemo(
    () => workspace.tasks.filter((task) => draft ? task.parent_id === draft.id : false),
    [draft?.id, workspace.tasks],
  )
  const dependentTasks = useMemo(
    () => workspace.tasks.filter((task) => draft ? task.dependencies.includes(draft.id) : false),
    [draft?.id, workspace.tasks],
  )
  const cyclicParentCandidates = useMemo(
    () => draft ? cyclicRelationshipCandidates(workspace.tasks, draft.id, 'parent_id') : new Set<string>(),
    [draft?.id, workspace.tasks],
  )
  const cyclicDependencyCandidates = useMemo(
    () => draft ? cyclicRelationshipCandidates(workspace.tasks, draft.id, 'dependencies') : new Set<string>(),
    [draft?.id, workspace.tasks],
  )
  const availableParentTasks = useMemo(
    () => workspace.tasks.filter((task) => draft
      && task.id !== draft.id
      && (task.id === draft.parent_id || !cyclicParentCandidates.has(task.id))),
    [cyclicParentCandidates, draft?.id, draft?.parent_id, workspace.tasks],
  )
  const availableDependencyTasks = useMemo(
    () => workspace.tasks.filter((task) => draft
      && task.id !== draft.id
      && !draft.dependencies.includes(task.id)
      && !cyclicDependencyCandidates.has(task.id)),
    [cyclicDependencyCandidates, draft?.dependencies, draft?.id, workspace.tasks],
  )
  const linkedMicrosoftSources = useMemo(
    () => detailQuery.data?.context.map(microsoftReplySource).filter((source): source is ReplySource => source !== null) ?? [],
    [detailQuery.data?.context],
  )
  const replySources = useMemo(
    () => linkedMicrosoftSources.filter((source) => providerReplyVerified(source.provider, providerGates)),
    [linkedMicrosoftSources, providerGates],
  )
  const replyUnavailableSources = useMemo(
    () => linkedMicrosoftSources.filter((source) => !providerReplyVerified(source.provider, providerGates)),
    [linkedMicrosoftSources, providerGates],
  )

  const recordReply = (updated: ReplyCommand, message: string) => {
    queryClient.setQueryData(['task', taskId], (current: typeof detailQuery.data) => current ? {
      ...current,
      replies: [...current.replies.filter((reply) => reply.id !== updated.id), updated],
    } : current)
    onNotice(message)
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
      <header className="drawer-header">
        <div className="drawer-header__identity"><span className="task-glyph"><Icon name="task" size={17} /></span><div><span>Task</span><strong>{taskId}</strong></div></div>
        <div className="drawer-header__actions">
          <span aria-live="polite" className={`save-state save-state--${saveState}`}>
            {saveState === 'saving' ? 'Saving…' : saveState === 'saved' ? 'Saved' : saveState === 'error' ? 'Not saved' : ''}
          </span>
          <IconButton
            disabled={navigationLocked}
            icon="more"
            label="More task actions"
            onClick={() => setTaskActionsOpen(true)}
            variant="ghost"
          />
          <IconButton disabled={navigationLocked} icon="close" label="Close task drawer" onClick={() => navigateAfterSave(onClose)} variant="ghost" />
        </div>
      </header>

      <nav aria-label="Task details" className="drawer-tabs">
        {(['overview', 'context', 'activity'] as const).map((item) => (
          <button aria-selected={tab === item} className={tab === item ? 'is-active' : ''} key={item} onClick={() => setTab(item)} role="tab" type="button">
            {item[0].toUpperCase() + item.slice(1)}
            {item === 'context' && draft?.context_count ? <span>{draft.context_count}</span> : null}
          </button>
        ))}
      </nav>

      <div className="drawer-body">
        {detailQuery.isPending ? <LoadingBlock label="Loading task details…" /> : detailQuery.isError ? (
          <EmptyState icon="warning" title="Task detail unavailable">{getErrorMessage(detailQuery.error)}</EmptyState>
        ) : !draft ? <EmptyState icon="warning" title="Task detail unavailable">The task did not include a readable projection.</EmptyState> : null}

        {draft && tab === 'overview' ? (
          <div className="drawer-overview">
            <label className="drawer-title-field">
              <span className="sr-only">Task title</span>
              <textarea
                disabled={saveState === 'saving'}
                onBlur={() => {
                  if (draft.title.trim()) {
                    void save({ title: draft.title.trim() })
                  } else {
                    setSaveState('error')
                    setSaveError('Task title cannot be empty. Restore a title or discard the unsaved change.')
                  }
                }}
                onChange={(event) => { markDirty('title'); setDraft({ ...draft, title: event.target.value }) }}
                rows={2}
                value={draft.title}
              />
            </label>
            <div className="drawer-chips">
              <Pill tone={draft.priority.toLowerCase()}>{draft.priority} · {priorityLabels[draft.priority]}</Pill>
              {taskObjectives.map((objective) => onOpenObjective ? (
                <button
                  aria-label={`Open objective ${objective.id}`}
                  className="objective-pill-link"
                  disabled={navigationLocked}
                  key={objective.id}
                  onClick={() => navigateAfterSave(() => onOpenObjective(objective.id))}
                  title={getObjectiveTitle(objective)}
                  type="button"
                ><Pill tone="accent">{objective.id}</Pill></button>
              ) : <Pill key={objective.id} tone="accent">{objective.id}</Pill>)}
            </div>
            <div aria-label="Task identity" className="task-identity" role="group">
              <span><strong>Stable UID</strong><code>{draft.uid}</code></span>
              <span><strong>Current version</strong><code>Revision {draft.revision}</code></span>
            </div>
            <section className="snapshot-launch">
              <div>
                <strong>Execution handoff</strong>
                <p>Review and save one immutable planning-task revision for Conduit.</p>
              </div>
              <Button
                disabled={navigationLocked}
                icon="arrowUpRight"
                onClick={() => setSnapshotOpen(true)}
                variant="secondary"
              >Export to Conduit</Button>
            </section>

            {saveError ? (
              <div className="inline-error" role="alert">
                <span>{saveError}</span>
                {saveState === 'error' && hasPatch(saveRunRef.current.queued) ? <Button onClick={() => void retrySave()} variant="ghost">Retry save</Button> : null}
                {saveState === 'error' ? <Button onClick={discardUnsavedChanges} variant="ghost">Discard unsaved changes</Button> : null}
              </div>
            ) : null}

            <section className="drawer-section">
              <h3>Properties</h3>
              <div className="property-grid">
                <label><span>Status</span><select disabled={saveState === 'saving'} onChange={(event) => { const status = event.target.value as Task['status']; setDraft({ ...draft, status }); void save({ status }) }} value={draft.status}>{TASK_STATUSES.map((status) => <option key={status} value={status}>{statusLabels[status]}</option>)}</select></label>
                <label><span>Priority</span><select disabled={saveState === 'saving'} onChange={(event) => { const priority = event.target.value as Task['priority']; setDraft({ ...draft, priority }); void save({ priority }) }} value={draft.priority}>{TASK_PRIORITIES.map((priority) => <option key={priority} value={priority}>{priority} · {priorityLabels[priority]}</option>)}</select></label>
                <label><span>Plan for</span><input disabled={saveState === 'saving'} onBlur={() => { void save({ scheduled: draft.scheduled ?? null }) }} onChange={(event) => { markDirty('scheduled'); setDraft({ ...draft, scheduled: event.target.value || null }) }} type="date" value={draft.scheduled ?? ''} /></label>
                <label><span>Due</span><input disabled={saveState === 'saving'} onBlur={() => { void save({ due: draft.due }) }} onChange={(event) => { markDirty('due'); setDraft({ ...draft, due: event.target.value || null }) }} type="date" value={draft.due ?? ''} /></label>
                <label><span>Estimate <small>minutes</small></span><input disabled={saveState === 'saving'} max={1440} min={1} onBlur={() => { void save({ estimate_minutes: draft.estimate_minutes ?? null }) }} onChange={(event) => { markDirty('estimate_minutes'); setDraft({ ...draft, estimate_minutes: event.target.value ? Number(event.target.value) : null }) }} type="number" value={draft.estimate_minutes ?? ''} /></label>
                <label><span>Parent</span><select disabled={saveState === 'saving'} onChange={(event) => { const parent_id = event.target.value || null; setDraft({ ...draft, parent_id }); void save({ parent_id }) }} value={draft.parent_id ?? ''}><option value="">No parent</option>{availableParentTasks.map((task) => <option key={task.id} value={task.id}>{task.id} · {task.title}</option>)}</select></label>
              </div>
            </section>

            <section className="drawer-section">
              <h3>Definition of done</h3>
              <textarea
                className="drawer-detail-input"
                disabled={saveState === 'saving'}
                onBlur={() => { void save({ detail: draft.detail }) }}
                onChange={(event) => { markDirty('detail'); setDraft({ ...draft, detail: event.target.value }) }}
                placeholder="Describe the outcome…"
                rows={5}
                value={draft.detail}
              />
            </section>

            <section className="drawer-section">
              <h3>Alignment</h3>
              <div className="objective-checks">
                {workspace.objectives.map((objective) => (
                  <label key={objective.id}>
                    <input
                      checked={draft.objective_ids.includes(objective.id)}
                      disabled={saveState === 'saving'}
                      onChange={() => {
                        const objective_ids = draft.objective_ids.includes(objective.id)
                          ? draft.objective_ids.filter((id) => id !== objective.id)
                          : [...draft.objective_ids, objective.id]
                        setDraft({ ...draft, objective_ids })
                        void save({ objective_ids })
                      }}
                      type="checkbox"
                    />
                    <span><strong>{objective.id}</strong>{getObjectiveTitle(objective)}</span>
                  </label>
                ))}
              </div>
            </section>

            {onOpenTask && (parentTask || dependencyTasks.length || childTasks.length || dependentTasks.length) ? (
              <section className="drawer-section">
                <h3>Task relationships</h3>
                <div className="task-relationship-links">
                  {parentTask ? (
                    <button
                      aria-label={`Open parent ${parentTask.id}`}
                      disabled={navigationLocked}
                      onClick={() => navigateAfterSave(() => onOpenTask(parentTask.id))}
                      type="button"
                    ><span>Parent</span><strong>{parentTask.id}</strong><small>{parentTask.title}</small></button>
                  ) : null}
                  {dependencyTasks.map((dependency) => (
                    <button
                      aria-label={`Open dependency ${dependency.id}`}
                      disabled={navigationLocked}
                      key={dependency.id}
                      onClick={() => navigateAfterSave(() => onOpenTask(dependency.id))}
                      type="button"
                    ><span>Dependency</span><strong>{dependency.id}</strong><small>{dependency.title}</small></button>
                  ))}
                  {childTasks.map((child) => (
                    <button
                      aria-label={`Open child ${child.id}`}
                      disabled={navigationLocked}
                      key={child.id}
                      onClick={() => navigateAfterSave(() => onOpenTask(child.id))}
                      type="button"
                    ><span>Child</span><strong>{child.id}</strong><small>{child.title}</small></button>
                  ))}
                  {dependentTasks.map((dependent) => (
                    <button
                      aria-label={`Open dependent ${dependent.id}`}
                      disabled={navigationLocked}
                      key={dependent.id}
                      onClick={() => navigateAfterSave(() => onOpenTask(dependent.id))}
                      type="button"
                    ><span>Dependent</span><strong>{dependent.id}</strong><small>{dependent.title}</small></button>
                  ))}
                </div>
              </section>
            ) : null}

            <section className="drawer-section">
              <h3>Tags & dependencies</h3>
              <label className="field"><span>Tags</span><input disabled={saveState === 'saving'} onBlur={() => { const tags = tagText.split(',').map((value) => value.trim()).filter(Boolean); setDraft({ ...draft, tags }); void save({ tags }) }} onChange={(event) => { const value = event.target.value; markDirty('tags'); setTagText(value); setDraft({ ...draft, tags: value.split(',').map((item) => item.trim()).filter(Boolean) }) }} value={tagText} /></label>
              <div className="dependency-editor">
                <span className="dependency-editor__label">Dependencies</span>
                {draft.dependencies.length ? (
                  <div className="dependency-chips">
                    {draft.dependencies.map((dependencyId) => {
                      const dependency = workspace.tasks.find((task) => task.id === dependencyId)
                      return (
                        <button
                          aria-label={`Remove dependency ${dependencyId}`}
                          disabled={saveState === 'saving'}
                          key={dependencyId}
                          onClick={() => {
                            const dependencies = draft.dependencies.filter((id) => id !== dependencyId)
                            setDraft({ ...draft, dependencies })
                            void save({ dependencies })
                          }}
                          type="button"
                        ><strong>{dependencyId}</strong><span>{dependency?.title ?? 'Unavailable Task'}</span><Icon name="close" size={12} /></button>
                      )
                    })}
                  </div>
                ) : <p className="dependency-editor__empty">No dependencies</p>}
                <label className="field"><span>Add dependency</span><select
                  aria-label="Add dependency"
                  disabled={saveState === 'saving' || availableDependencyTasks.length === 0}
                  onChange={(event) => {
                    if (!event.target.value) return
                    const dependencies = [...new Set([...draft.dependencies, event.target.value])].sort()
                    setDraft({ ...draft, dependencies })
                    void save({ dependencies })
                  }}
                  value=""
                ><option value="">Choose a Task…</option>{availableDependencyTasks.map((task) => <option key={task.id} value={task.id}>{task.id} · {task.title}</option>)}</select></label>
              </div>
            </section>
          </div>
        ) : null}

        {draft && tab === 'context' ? (
          <div className="context-tab">
            {replySources.length ? (
              <section className="reply-launch">
                <div><strong>Reply to a linked Microsoft thread</strong><p>The target comes only from the selected Capture. Approval creates a command; sending remains a manual agent handoff.</p></div>
                {detailQuery.data?.replies.length ? <div className="reply-launch__states">{detailQuery.data.replies.map((reply) => <Pill key={reply.id} tone={reply.state}>{reply.state}</Pill>)}</div> : null}
                <Button icon="command" onClick={() => setReplyOpen((value) => !value)} variant={replyOpen ? 'ghost' : 'primary'}>{replyOpen ? 'Close reply composer' : 'Prepare Outlook/Teams reply'}</Button>
              </section>
            ) : null}
            {replyUnavailableSources.length ? (
              <section className="reply-launch reply-launch--unavailable">
                <div><strong>Microsoft reply unavailable</strong><p>{replyUnavailableSources.map((source) => source.display_title).join(', ')} cannot be used for replies until that provider’s read and reply capabilities pass Gate 0.</p></div>
                <Pill tone="neutral">Reply unavailable · Gate 0 pending</Pill>
              </section>
            ) : null}
            {replyOpen && replySources.length ? (
              <ReplyComposer
                onCreate={async (input) => {
                  const created = await api.createReply(input)
                  recordReply(created, `${created.id} approved; copy it to the connected agent when ready`)
                  await queryClient.invalidateQueries({ queryKey: ['task', taskId] })
                  return created
                }}
                onImportReceipt={async (replyId, receipt) => {
                  const updated = await api.importReplyReceipt(replyId, receipt)
                  recordReply(updated, `${updated.id} receipt recorded as ${updated.state}`)
                  await queryClient.invalidateQueries({ queryKey: ['task', taskId] })
                  return updated
                }}
                replies={detailQuery.data?.replies ?? []}
                sources={replySources}
                taskId={taskId}
              />
            ) : null}
            <div className="timeline-list">
              {detailQuery.data?.context.length ? detailQuery.data.context.map((item, index) => {
                const external = externalContext(item)
                const source = item.source
                const normalized = item.normalized
                const microsoftProvider = source && MICROSOFT_PROVIDERS.includes(source.provider as MicrosoftProvider)
                  ? source.provider as MicrosoftProvider
                  : null
                const replyUnavailable = microsoftProvider !== null
                  && !providerReplyVerified(microsoftProvider, providerGates)
                return (
                  <article className="context-entry" key={item.id ?? index}>
                    <span className={`timeline-mark ${external ? 'timeline-mark--external' : ''}`}><Icon name={external ? 'inbox' : 'context'} size={14} /></span>
                    <div>
                      <header><Pill tone={external ? 'verified' : 'neutral'}>{external ? source?.provider ?? 'External context' : 'Context card'}</Pill>{replyUnavailable ? <Pill tone="neutral">Reply unavailable · Gate 0 pending</Pill> : null}<time>{formatDateTime(item.created_at ?? item.created)}</time></header>
                      <h3>{contextTitle(item)}</h3>
                      {normalized?.context ? <p>{normalized.context}</p> : item.text ? <p>{item.text}</p> : null}
                      {normalized?.action_items?.length ? <ul>{normalized.action_items.map((action, actionIndex) => <li key={action.id ?? actionIndex}>{action.title}</li>)}</ul> : null}
                      {safeExternalUrl(source?.web_url) ? <a href={safeExternalUrl(source?.web_url)!} rel="noopener noreferrer" target="_blank">Open source <Icon name="arrowUpRight" size={13} /></a> : null}
                    </div>
                  </article>
                )
              }) : (
                <EmptyState icon="context" title="No context yet">Link a sanitized Inbox capture or add a Context card to preserve why this work matters.</EmptyState>
              )}
            </div>
          </div>
        ) : null}

        {draft && tab === 'activity' ? (
          <div className="timeline-list">
            {detailQuery.data?.activity.length ? detailQuery.data.activity.map((item, index) => (
              <article className="activity-entry" key={item.id ?? index}>
                <span className="timeline-mark"><Icon name="activity" size={14} /></span>
                <div>
                  <h3>{activityTitle(item)}</h3>
                  <time>{formatDateTime(item.created_at ?? item.at)}</time>
                  {item.type === 'task.planning_status' && item.prior_revision !== null && item.prior_revision !== undefined
                    ? <p>Revision {item.prior_revision} → {item.new_revision}</p>
                    : null}
                  {item.actor ? <p>By {item.actor}{item.provenance ? ` · ${item.provenance}` : ''}</p> : null}
                </div>
              </article>
            )) : (
              <EmptyState icon="activity" title="No activity yet">Changes to status and linked context will appear here.</EmptyState>
            )}
          </div>
        ) : null}
      </div>
      <SnapshotExportDialog
        onClose={() => setSnapshotOpen(false)}
        onNotice={onNotice}
        open={snapshotOpen}
        taskId={taskId}
      />
      {draft ? (
        <TaskActionsDialog
          onClose={() => setTaskActionsOpen(false)}
          onNotice={onNotice}
          onSaved={recordTaskAction}
          open={taskActionsOpen}
          task={draft}
        />
      ) : null}
    </aside>
  )
}
