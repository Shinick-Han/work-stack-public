import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, CommitUnknownError } from '../api/client'
import { Icon } from '../components/Icon'
import { Button, ErrorState, IconButton, LoadingBlock } from '../components/Primitives'
import { microsoftProviderGates, type MicrosoftProviderGates } from '../config/providerGates'
import type { Capture, CapturePacket, QuickTaskInput, TaskStatus, WorkSessionEntryInput, WorkspaceProjection } from '../domain/types'
import { CommandPalette } from '../features/commands/CommandPalette'
import { CaptureDrawer } from '../features/inbox/CaptureDrawer'
import { CaptureImportDialog } from '../features/inbox/CaptureImportDialog'
import { InboxPage } from '../features/inbox/InboxPage'
import { buildManualWebCapturePacket } from '../features/inbox/sourceCapture'
import { FocusPage } from '../features/focus/FocusPage'
import { useLocalToday } from '../features/focus/useLocalToday'
import { MicrosoftOobDialog, type MicrosoftOobMode } from '../features/integrations/MicrosoftOobDialog'
import { ObjectiveHubPage } from '../features/objectives/ObjectiveHubPage'
import { DailyReviewPage } from '../features/review/DailyReviewPage'
import { QuickTaskDialog } from '../features/tasks/QuickTaskDialog'
import { WorkspaceActionsDialog } from '../features/workspace/WorkspaceActionsDialog'
import { filterWorkspaceTasks } from '../features/workspace/views/viewModels'
import { getErrorMessage, getObjectiveTitle, statusLabels } from '../utils/format'
import { useUrlState } from './urlState'
import { subscribePlanningChanges } from './crossTabSync'
import { SyncStatusControl, SyncStatusDialog, isSyncUnavailable, isSyncWriteBlocked } from './SyncStatusControl'
import { UpdateStatusControl } from './UpdateStatusControl'
import { subscribeSyncStatusEvents } from './syncStatusEvents'
import { applyTheme, readTheme, type WorkStackTheme } from './theme'

const TaskDrawer = lazy(() => import('../features/tasks/TaskDrawer').then((module) => ({ default: module.TaskDrawer })))
const WorkspacePage = lazy(() => import('../features/workspace/WorkspacePage').then((module) => ({ default: module.WorkspacePage })))

interface Notice {
  action?: { label: string; run: () => void }
  id: number
  message: string
  tone: 'success' | 'error'
}

interface AppProps {
  providerGates?: MicrosoftProviderGates
}

function FocusSurface({
  isRefreshing,
  onChangeTaskStatus,
  onCreateTask,
  onRefresh,
  onSelectTask,
  onNotice,
  workspace,
}: {
  isRefreshing: boolean
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  onCreateTask: () => void
  onRefresh: () => void
  onSelectTask: (taskId: string) => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  workspace: WorkspaceProjection
}) {
  const today = useLocalToday()
  const queryClient = useQueryClient()
  const workSessionsQuery = useQuery({
    queryKey: ['work-sessions'],
    queryFn: () => api.getWorkSessions(),
  })
  const workSessionMutation = useMutation({
    mutationFn: async (operation:
      | { kind: 'start'; taskId: string }
      | { kind: 'transition'; sessionId: string; action: 'pause' | 'resume' | 'stop' }
      | { kind: 'record'; sessionId: string; input: WorkSessionEntryInput }
    ) => {
      if (operation.kind === 'start') return { kind: operation.kind, result: await api.startWorkSession(operation.taskId) }
      if (operation.kind === 'transition') return { kind: operation.kind, action: operation.action, result: await api.transitionWorkSession(operation.sessionId, operation.action) }
      return { kind: operation.kind, result: await api.recordWorkSession(operation.sessionId, operation.input) }
    },
    onSuccess: async (completed) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['work-sessions'] }),
        queryClient.invalidateQueries({ queryKey: ['review'] }),
      ])
      const message = completed.kind === 'start'
        ? 'Work session started'
        : completed.kind === 'record'
          ? 'Worklog entry recorded'
          : completed.action === 'pause'
            ? 'Work session paused'
            : completed.action === 'resume'
              ? 'Work session resumed'
              : 'Work session stopped — review the Worklog draft'
      onNotice(message)
    },
    onError: (error) => onNotice(getErrorMessage(error), 'error'),
  })

  return (
    <FocusPage
      isRefreshing={isRefreshing}
      onChangeTaskStatus={onChangeTaskStatus}
      onCreateTask={onCreateTask}
      onRefresh={onRefresh}
      onNotice={onNotice}
      onSelectTask={onSelectTask}
      isWorkSessionPending={workSessionMutation.isPending}
      onRecordWorkSession={(sessionId, input) => workSessionMutation.mutateAsync({ kind: 'record', sessionId, input }).then(() => undefined)}
      onStartWorkSession={(taskId) => workSessionMutation.mutateAsync({ kind: 'start', taskId }).then(() => undefined)}
      onTransitionWorkSession={(sessionId, action) => workSessionMutation.mutateAsync({ kind: 'transition', sessionId, action }).then(() => undefined)}
      today={today}
      workSessionError={workSessionsQuery.isError ? getErrorMessage(workSessionsQuery.error) : undefined}
      workSessions={workSessionsQuery.data}
      workspace={workspace}
    />
  )
}

function ReviewSurface({
  captures,
  onNotice,
  onOpenCapture,
  onOpenTask,
  workspace,
}: {
  captures: Capture[]
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onOpenCapture: (captureId: string) => void
  onOpenTask: (taskId: string) => void
  workspace: WorkspaceProjection
}) {
  const today = useLocalToday()
  return <DailyReviewPage captures={captures} onNotice={onNotice} onOpenCapture={onOpenCapture} onOpenTask={onOpenTask} today={today} workspace={workspace} />
}

export function App({ providerGates = microsoftProviderGates }: AppProps) {
  const queryClient = useQueryClient()
  const pendingStatusMutations = useRef(0)
  const taskNavigationLockRef = useRef(false)
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false)
  const [quickTaskOpen, setQuickTaskOpen] = useState(false)
  const [quickTaskInitialObjectiveId, setQuickTaskInitialObjectiveId] = useState('')
  const [quickTaskDraftResetToken, setQuickTaskDraftResetToken] = useState(0)
  const [workspaceActionsOpen, setWorkspaceActionsOpen] = useState(false)
  const [sidebarTaskQuery, setSidebarTaskQuery] = useState('')
  const [importOpen, setImportOpen] = useState(false)
  const [microsoftOobMode, setMicrosoftOobMode] = useState<MicrosoftOobMode | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [syncReviewOpen, setSyncReviewOpen] = useState(false)
  const [theme, setTheme] = useState<WorkStackTheme>(readTheme)

  const openQuickTask = (objectiveId = '') => {
    setQuickTaskInitialObjectiveId(objectiveId)
    setQuickTaskOpen(true)
  }

  const showNotice = (
    message: string,
    tone: Notice['tone'] = 'success',
    action?: Notice['action'],
  ) => {
    setNotice({ action, id: Date.now(), message, tone })
  }

  const { state, update } = useUrlState((current, next) => {
    if (taskNavigationLockRef.current && current.taskId && next.taskId !== current.taskId) {
      showNotice('Finish saving or discard the unsaved Task changes before leaving.', 'error')
      return false
    }
    return true
  })

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice((current) => current?.id === notice.id ? null : current), 3200)
    return () => window.clearTimeout(timer)
  }, [notice])

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      const target = event.target
      const isEditableTarget = target instanceof Element && target.matches('input, textarea, select')
      if (event.key === '/' && !isEditableTarget) {
        const searchInput = document.querySelector<HTMLInputElement>('.search-control input')
        if (searchInput) {
          event.preventDefault()
          searchInput.focus()
        }
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setCommandPaletteOpen(true)
      }
    }
    window.addEventListener('keydown', handleShortcut)
    return () => window.removeEventListener('keydown', handleShortcut)
  }, [])

  const workspaceQuery = useQuery({
    queryKey: ['workspace'],
    queryFn: api.getWorkspace,
  })

  const syncStatusQuery = useQuery({
    queryKey: ['sync-status'],
    queryFn: api.getSyncStatus,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const syncAdoptMutation = useMutation({
    mutationFn: ({ generation, manifestDigest }: { generation: number; manifestDigest: string }) => (
      api.adoptSyncChanges(generation, manifestDigest)
    ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['sync-status'] }),
        queryClient.invalidateQueries({ queryKey: ['workspace'] }),
        queryClient.invalidateQueries({ queryKey: ['captures'] }),
        queryClient.invalidateQueries({ queryKey: ['task'] }),
        queryClient.invalidateQueries({ queryKey: ['review'] }),
      ])
      setSyncReviewOpen(false)
      showNotice('Validated SSOT changes accepted')
    },
  })

  const capturesQuery = useQuery({
    queryKey: ['captures', 'all'],
    queryFn: () => api.getCaptures('all'),
  })

  const quickTaskMutation = useMutation({
    mutationFn: (input: QuickTaskInput) => api.createTask(input),
    onSuccess: (created) => {
      setQuickTaskDraftResetToken((token) => token + 1)
      setQuickTaskInitialObjectiveId('')
      void queryClient.invalidateQueries({ queryKey: ['workspace'] }).catch(() => undefined)
      setQuickTaskOpen(false)
      if (state.surface === 'inbox') {
        update({ surface: 'workspace', captureId: null, taskId: created.id })
      } else {
        update({ captureId: null, taskId: created.id })
      }
      showNotice(`${created.id} created`)
    },
    onError: (error) => {
      if (!(error instanceof CommitUnknownError)) return
      setQuickTaskInitialObjectiveId('')
      setQuickTaskOpen(false)
      update({ surface: 'workspace', captureId: null, taskId: null })
      void queryClient.invalidateQueries({ queryKey: ['workspace'] }).catch(() => undefined)
      showNotice('Task may have been created. Check Workspace before trying again.', 'error')
    },
  })

  const importMutation = useMutation({
    mutationFn: (packet: CapturePacket) => api.ingestCapture(packet),
    onSuccess: async (capture) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['captures'] }),
        queryClient.invalidateQueries({ queryKey: ['workspace'] }),
      ])
      setImportOpen(false)
      update({ surface: 'inbox', captureId: capture.id, taskId: null })
      showNotice(`Imported ${capture.id}`)
    },
  })

  useEffect(() => subscribePlanningChanges(() => {
    void queryClient.invalidateQueries()
  }), [queryClient])

  const refreshAuthoritativeState = () => {
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ['sync-status'] }),
      queryClient.invalidateQueries({ queryKey: ['workspace'] }),
      queryClient.invalidateQueries({ queryKey: ['captures'] }),
      queryClient.invalidateQueries({ queryKey: ['task'] }),
      queryClient.invalidateQueries({ queryKey: ['review'] }),
    ])
  }

  useEffect(() => subscribeSyncStatusEvents(refreshAuthoritativeState), [queryClient])

  const workspaceActionMutation = useMutation({
    mutationFn: async (action: { text: string; links: string[]; idempotencyKey: string }) => {
      const created = await api.createNote(action.text, action.links, action.idempotencyKey)
      return { id: created.id, label: 'Context card' }
    },
    onSuccess: (created) => {
      setWorkspaceActionsOpen(false)
      showNotice(`${created.label} ${created.id} added`)
      void queryClient.invalidateQueries({ queryKey: ['workspace'] })
    },
  })

  const oobImportMutation = useMutation({
    mutationFn: async (packets: CapturePacket[]) => {
      const imported = []
      for (const [index, packet] of packets.entries()) {
        try {
          imported.push(await api.ingestCapture(packet))
        } catch (error) {
          throw new Error(`Agent result ${index + 1} was not imported: ${getErrorMessage(error)}`)
        }
      }
      return imported
    },
    onSuccess: async (captures) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['captures'] }),
        queryClient.invalidateQueries({ queryKey: ['workspace'] }),
      ])
      setMicrosoftOobMode(null)
      const selected = captures.at(-1)
      if (selected) update({ surface: 'inbox', captureId: selected.id, taskId: null })
      showNotice(`Imported ${captures.length} Microsoft 365 capture${captures.length === 1 ? '' : 's'}`)
    },
    onError: async () => {
      // Earlier packets in a multi-result handoff may already have been accepted.
      // Refresh both projections so a retry remains understandable and idempotent.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['captures'] }),
        queryClient.invalidateQueries({ queryKey: ['workspace'] }),
      ])
    },
  })

  const undoStatusMutation = useMutation({
    mutationKey: ['task-status-undo'],
    mutationFn: ({ taskId, status, revision }: { taskId: string; status: TaskStatus; revision: number }) => (
      api.patchTask(taskId, { status, revision })
    ),
    onSuccess: (updated) => {
      queryClient.setQueryData<WorkspaceProjection>(['workspace'], (current) => current ? {
        ...current,
        tasks: current.tasks.map((task) => task.id === updated.id ? updated : task),
      } : current)
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ['workspace'] }),
        queryClient.invalidateQueries({ queryKey: ['task', updated.id] }),
      ])
      showNotice(`${updated.id} restored to ${updated.status}`)
    },
    onError: (error, { taskId }) => {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ['workspace'] }),
        queryClient.invalidateQueries({ queryKey: ['task', taskId] }),
      ])
      showNotice(`Undo was not applied: ${getErrorMessage(error)}`, 'error')
    },
  })

  const statusMutation = useMutation({
    mutationKey: ['task-status'],
    mutationFn: ({ taskId, status, revision }: { taskId: string; status: TaskStatus; revision: number }) => (
      api.patchTask(taskId, { status, revision })
    ),
    onMutate: async ({ taskId, status }) => {
      pendingStatusMutations.current += 1
      await queryClient.cancelQueries({ queryKey: ['workspace'] })
      const previousTask = queryClient
        .getQueryData<WorkspaceProjection>(['workspace'])
        ?.tasks.find((task) => task.id === taskId) ?? null
      queryClient.setQueryData<WorkspaceProjection>(['workspace'], (current) => current ? {
        ...current,
        tasks: current.tasks.map((task) => task.id === taskId ? { ...task, status } : task),
      } : current)
      return { previousTask, optimisticStatus: status }
    },
    onError: (error, { taskId }, context) => {
      const previousTask = context?.previousTask
      if (previousTask) {
        queryClient.setQueryData<WorkspaceProjection>(['workspace'], (current) => current ? {
          ...current,
          tasks: current.tasks.map((task) => (
            task.id === taskId
              && task.status === context.optimisticStatus
              && task.revision === previousTask.revision
              ? previousTask
              : task
          )),
        } : current)
      }
      showNotice(getErrorMessage(error), 'error')
    },
    onSuccess: (updated, _variables, context) => {
      queryClient.setQueryData<WorkspaceProjection>(['workspace'], (current) => current ? {
        ...current,
        tasks: current.tasks.map((task) => task.id === updated.id ? updated : task),
      } : current)
      queryClient.invalidateQueries({ queryKey: ['task', updated.id] })
      const previous = context?.previousTask
      showNotice(
        `${updated.id} moved to ${updated.status}`,
        'success',
        previous && previous.status !== updated.status ? {
          label: 'Undo',
          run: () => {
            setNotice(null)
            undoStatusMutation.mutate({
              taskId: updated.id,
              status: previous.status,
              revision: updated.revision,
            })
          },
        } : undefined,
      )
    },
    onSettled: () => {
      pendingStatusMutations.current = Math.max(0, pendingStatusMutations.current - 1)
      if (pendingStatusMutations.current === 0) {
        return queryClient.invalidateQueries({ queryKey: ['workspace'] })
      }
    },
  })

  const changeTaskStatus = async (taskId: string, status: TaskStatus): Promise<void> => {
    const task = queryClient
      .getQueryData<WorkspaceProjection>(['workspace'])
      ?.tasks.find((item) => item.id === taskId)
    if (!task) throw new Error(`Task ${taskId} is no longer in the workspace.`)
    await statusMutation.mutateAsync({ taskId, status, revision: task.revision })
  }

  const runCaptureAction = async (operation: () => Promise<unknown>, successMessage: string) => {
    await operation()
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['captures'] }),
      queryClient.invalidateQueries({ queryKey: ['workspace'] }),
    ])
    showNotice(successMessage)
  }

  const selectedCapture = useMemo(
    () => capturesQuery.data?.find((capture) => capture.id === state.captureId) ?? null,
    [capturesQuery.data, state.captureId],
  )

  const drawerOpen = Boolean(state.taskId || selectedCapture)
  const workspace = workspaceQuery.data
  const syncStatus = syncStatusQuery.data
  // Only a confirmed 404 from a healthy legacy/local server is optional. Every
  // transport error and every other HTTP failure is fail-closed, including the
  // first status request.
  const legacySyncEndpointUnavailable = syncStatusQuery.error instanceof ApiError
    && syncStatusQuery.error.status === 404
    && !syncStatus
    && !workspaceQuery.isError
  const effectiveSyncError = syncStatusQuery.error && !legacySyncEndpointUnavailable
    ? syncStatusQuery.error
    : null
  const syncEndpointDisconnected = Boolean(effectiveSyncError && !isSyncUnavailable(effectiveSyncError))
  const syncWriteBlocked = isSyncWriteBlocked(syncStatus, effectiveSyncError)
  useEffect(() => {
    const drawers = Array.from(document.querySelectorAll<HTMLElement>('.detail-drawer'))
    for (const drawer of drawers) drawer.inert = syncWriteBlocked
    if (syncWriteBlocked) {
      setQuickTaskOpen(false)
      setWorkspaceActionsOpen(false)
      setImportOpen(false)
      setMicrosoftOobMode(null)
    }
    return () => {
      for (const drawer of drawers) drawer.inert = false
    }
  }, [selectedCapture?.id, state.taskId, syncWriteBlocked])
  const sidebarTaskResults = useMemo(() => {
    const query = sidebarTaskQuery.trim().toLocaleLowerCase()
    const matches = (workspace?.tasks ?? []).filter((task) => (
      !query
      || `${task.id} ${task.title} ${task.status} ${task.priority}`.toLocaleLowerCase().includes(query)
    ))
    return { total: matches.length, visible: matches.slice(0, 50) }
  }, [sidebarTaskQuery, workspace?.tasks])

  useEffect(() => {
    const handleWorkspaceShortcut = (event: KeyboardEvent) => {
      const target = event.target
      const isEditableTarget = target instanceof HTMLElement
        && (target.isContentEditable || target.matches('input, textarea, select'))
      if (
        event.defaultPrevented
        || event.altKey
        || event.ctrlKey
        || event.metaKey
        || event.shiftKey
        || isEditableTarget
        || document.querySelector('dialog[open]')
      ) return

      const numericTargets = {
        '1': { surface: 'workspace' as const, view: 'graph' as const },
        '2': { surface: 'workspace' as const, view: 'board' as const },
        '3': { surface: 'workspace' as const, view: 'treemap' as const },
        '4': { surface: 'focus' as const },
        '5': { surface: 'inbox' as const },
        '6': { surface: 'review' as const },
        '7': { surface: 'objectives' as const },
        '8': { surface: 'workspace' as const, view: 'table' as const },
      }
      const targetState = numericTargets[event.key as keyof typeof numericTargets]
      if (targetState) {
        event.preventDefault()
        update({ ...targetState, captureId: null, taskId: null })
        return
      }

      if (!workspace || !['j', 'k'].includes(event.key.toLowerCase())) return
      const navigableTasks = filterWorkspaceTasks(workspace.tasks, {
        search: state.search,
        status: state.status,
        priority: state.priority,
        objectiveId: state.objectiveId,
      })
      if (!navigableTasks.length || state.surface === 'inbox') return
      event.preventDefault()
      const currentIndex = navigableTasks.findIndex((task) => task.id === state.taskId)
      const direction = event.key.toLowerCase() === 'j' ? 1 : -1
      const nextIndex = currentIndex < 0
        ? (direction === 1 ? 0 : navigableTasks.length - 1)
        : (currentIndex + direction + navigableTasks.length) % navigableTasks.length
      update({ captureId: null, taskId: navigableTasks[nextIndex].id })
    }

    window.addEventListener('keydown', handleWorkspaceShortcut)
    return () => window.removeEventListener('keydown', handleWorkspaceShortcut)
  }, [state.objectiveId, state.priority, state.search, state.status, state.surface, state.taskId, update, workspace])

  return (
    <div className={`app-shell ${drawerOpen ? 'has-drawer' : ''} ${syncWriteBlocked ? 'is-write-blocked' : ''}`}>
      <aside className={`app-sidebar ${mobileNavOpen ? 'is-open' : ''}`}>
        <div className="brand-block">
          <span className="brand-mark"><span /><span /><span /></span>
          <div><strong>Work Stack</strong><small>Aligned execution</small></div>
          <IconButton className="mobile-close" icon="close" label="Close navigation" onClick={() => setMobileNavOpen(false)} variant="ghost" />
        </div>

        <nav aria-label="Product surfaces" className="primary-nav">
          <button
            aria-current={state.surface === 'workspace' ? 'page' : undefined}
            className={state.surface === 'workspace' ? 'is-active' : ''}
            onClick={() => { update({ surface: 'workspace', captureId: null }); setMobileNavOpen(false) }}
            type="button"
          ><Icon name="graph" /><span>Workspace</span><kbd>1–3 · 8</kbd></button>
          <button
            aria-current={state.surface === 'focus' ? 'page' : undefined}
            className={state.surface === 'focus' ? 'is-active' : ''}
            onClick={() => { update({ surface: 'focus', captureId: null }); setMobileNavOpen(false) }}
            type="button"
          ><Icon name="target" /><span>Focus</span><kbd>4</kbd></button>
          <button
            aria-current={state.surface === 'inbox' ? 'page' : undefined}
            className={state.surface === 'inbox' ? 'is-active' : ''}
            onClick={() => { update({ surface: 'inbox', taskId: null }); setMobileNavOpen(false) }}
            type="button"
          ><Icon name="inbox" /><span>Context Inbox</span><span className="nav-shortcut">{workspace?.inbox_count ? <b>{workspace.inbox_count}</b> : null}<kbd>5</kbd></span></button>
          <button
            aria-current={state.surface === 'review' ? 'page' : undefined}
            className={state.surface === 'review' ? 'is-active' : ''}
            onClick={() => { update({ surface: 'review', captureId: null }); setMobileNavOpen(false) }}
            type="button"
          ><Icon name="activity" /><span>Daily Review</span><kbd>6</kbd></button>
          <button
            aria-current={state.surface === 'objectives' ? 'page' : undefined}
            className={state.surface === 'objectives' ? 'is-active' : ''}
            onClick={() => { update({ surface: 'objectives', captureId: null }); setMobileNavOpen(false) }}
            type="button"
          ><Icon name="target" /><span>Objective Hub</span><kbd>7</kbd></button>
        </nav>

        <details className="sidebar-section sidebar-objectives">
          <summary role="button"><span>Objectives</span><span>{workspace?.objectives.length ?? 0}</span><Icon name="chevronDown" size={14} /></summary>
          <div className="sidebar-section-body">
            <button className="sidebar-view-all" onClick={() => update({ surface: 'objectives', objectiveId: 'all' })} type="button">Open Objective Hub</button>
            <div className="objective-nav">
              {workspace?.objectives.map((objective, index) => {
                const count = workspace.tasks.filter((task) => task.objective_ids.includes(objective.id) && task.status !== 'done' && task.status !== 'dropped').length
                return (
                  <button
                    className={state.surface === 'objectives' && state.objectiveId === objective.id ? 'is-active' : ''}
                    key={objective.id}
                    onClick={() => { update({ surface: 'objectives', objectiveId: objective.id, captureId: null }); setMobileNavOpen(false) }}
                    type="button"
                  >
                    <span className={`objective-dot objective-dot--${(index % 5) + 1}`} />
                    <span><strong>{objective.id}</strong><small>{getObjectiveTitle(objective)}</small></span>
                    <em>{count}</em>
                  </button>
                )
              })}
            </div>
          </div>
        </details>

        <details className="sidebar-section sidebar-tasks">
          <summary role="button"><span>Tasks</span><span>{workspace?.tasks.length ?? 0}</span><Icon name="chevronDown" size={14} /></summary>
          <div className="sidebar-section-body">
            <button
              className="sidebar-view-all"
              onClick={() => { update({ surface: 'workspace', view: 'table', captureId: null, taskId: null }); setMobileNavOpen(false) }}
              type="button"
            >Open Table</button>
            <label className="sidebar-task-search">
              <span className="sr-only">Filter sidebar tasks</span>
              <Icon name="search" size={13} />
              <input
                maxLength={120}
                onChange={(event) => setSidebarTaskQuery(event.target.value)}
                placeholder="Find a Task…"
                type="search"
                value={sidebarTaskQuery}
              />
            </label>
            <div className="task-nav">
              {sidebarTaskResults.visible.map((task) => (
                <button
                  aria-label={`Open task ${task.id}: ${task.title}`}
                  className={state.taskId === task.id ? 'is-active' : ''}
                  key={task.id}
                  onClick={() => { update({ surface: 'workspace', captureId: null, taskId: task.id }); setMobileNavOpen(false) }}
                  type="button"
                >
                  <span className={`task-priority-dot task-priority-dot--${task.priority.toLowerCase()}`} />
                  <span><strong>{task.id}</strong><small>{task.title}</small></span>
                  <em>{task.priority} · {statusLabels[task.status]}</em>
                </button>
              ))}
              {!sidebarTaskResults.total ? <p className="sidebar-task-empty">No Tasks match.</p> : null}
            </div>
            {sidebarTaskResults.total > sidebarTaskResults.visible.length ? (
              <p className="sidebar-task-limit">Showing first 50 of {sidebarTaskResults.total}</p>
            ) : null}
          </div>
        </details>

        <div className="sidebar-local">
          <span className={`live-dot ${syncWriteBlocked ? 'live-dot--warning' : ''}`} />
          <div>
            <strong>{syncStatus || syncEndpointDisconnected ? 'SSOT workspace' : 'Local workspace'}</strong>
            <small>{syncStatus ? `Generation ${syncStatus.generation} · ${syncStatus.state}` : syncEndpointDisconnected ? 'Connection unavailable · writes blocked' : 'On this device · no background sync'}</small>
          </div>
        </div>
      </aside>

      {mobileNavOpen ? <button aria-label="Close navigation" className="sidebar-scrim" onClick={() => setMobileNavOpen(false)} type="button" /> : null}

      <div className="app-stage">
        <header className="app-topbar">
          <div className="topbar-left">
            <IconButton className="mobile-menu" icon="menu" label="Open navigation" onClick={() => setMobileNavOpen(true)} variant="ghost" />
            <span>{workspace?.workspace.name ?? 'Work Stack'}</span><span>/</span><strong>{state.surface === 'workspace' ? 'Workspace' : state.surface === 'focus' ? 'Focus' : state.surface === 'review' ? 'Daily Review' : state.surface === 'objectives' ? 'Objective Hub' : 'Context Inbox'}</strong>
          </div>
          <div className="topbar-actions">
            <UpdateStatusControl />
            <SyncStatusControl
              error={effectiveSyncError}
              isFetching={syncStatusQuery.isFetching}
              onRefresh={refreshAuthoritativeState}
              onReview={() => { syncAdoptMutation.reset(); setSyncReviewOpen(true) }}
              status={syncStatus}
            />
            <button className="command-button" onClick={() => setCommandPaletteOpen(true)} type="button"><Icon name="command" size={15} /> Search or jump <kbd>⌘ K</kbd></button>
            <IconButton icon={theme === 'dark' ? 'sun' : 'moon'} label={`Use ${theme === 'dark' ? 'light' : 'dark'} theme`} onClick={() => setTheme((current) => current === 'dark' ? 'light' : 'dark')} variant="ghost" />
            <IconButton icon="more" label="More workspace actions" onClick={() => setWorkspaceActionsOpen(true)} variant="ghost" />
          </div>
        </header>

        <main aria-disabled={syncWriteBlocked || undefined} className="app-main" inert={syncWriteBlocked || undefined}>
          {workspace && workspaceQuery.isError ? (
            <div aria-label="Workspace refresh failed" className="workspace-refresh-warning" role="status">
              <div><strong>Showing the last confirmed workspace</strong><span>{getErrorMessage(workspaceQuery.error)}</span></div>
              <Button disabled={workspaceQuery.isFetching} onClick={() => void workspaceQuery.refetch()} variant="secondary">{workspaceQuery.isFetching ? 'Retrying…' : 'Retry workspace refresh'}</Button>
            </div>
          ) : null}
          {workspaceQuery.isPending ? <LoadingBlock label="Opening your workspace…" /> : !workspace ? (
            <ErrorState message={getErrorMessage(workspaceQuery.error)} onRetry={() => void workspaceQuery.refetch()} />
          ) : state.surface === 'workspace' ? (
            <Suspense fallback={<LoadingBlock label="Opening workspace view…" />}>
              <WorkspacePage
                isRefreshing={workspaceQuery.isFetching}
                onChangeTaskStatus={changeTaskStatus}
                onCreateTask={() => openQuickTask()}
                onOpenObjectives={() => update({ surface: 'objectives', objectiveId: 'all', taskId: null, captureId: null })}
                onRefresh={() => void workspaceQuery.refetch()}
                state={state}
                updateUrl={update}
                workspace={workspace}
              />
            </Suspense>
          ) : state.surface === 'focus' ? (
            <FocusSurface
              isRefreshing={workspaceQuery.isFetching}
              onChangeTaskStatus={changeTaskStatus}
              onCreateTask={() => openQuickTask()}
              onRefresh={() => void workspaceQuery.refetch()}
              onSelectTask={(taskId) => update({ taskId, captureId: null })}
              onNotice={showNotice}
              workspace={workspace}
            />
          ) : state.surface === 'review' ? (
            <ReviewSurface
              captures={capturesQuery.data ?? []}
              onNotice={showNotice}
              onOpenCapture={(captureId) => update({ surface: 'inbox', captureId, taskId: null })}
              onOpenTask={(taskId) => update({ taskId, captureId: null })}
              workspace={workspace}
            />
          ) : state.surface === 'objectives' ? (
            <ObjectiveHubPage
              objectiveId={state.objectiveId}
              onCreateAlignedTask={(objectiveId) => openQuickTask(objectiveId)}
              onNotice={showNotice}
              onOpenTask={(taskId) => update({ taskId, captureId: null })}
              onSelectObjective={(objectiveId) => update({ objectiveId })}
              workspace={workspace}
            />
          ) : capturesQuery.isPending ? <LoadingBlock label="Loading sanitized context…" /> : capturesQuery.isError ? (
            <ErrorState message={getErrorMessage(capturesQuery.error)} onRetry={() => void capturesQuery.refetch()} />
          ) : (
            <InboxPage
              captures={capturesQuery.data ?? []}
              onCreateSourceTask={async (draft) => {
                const packet = await buildManualWebCapturePacket(draft)
                const capture = await api.ingestCapture(packet)
                try {
                  if (draft.taskId) {
                    await api.linkCapture(capture.id, draft.taskId)
                    showNotice(`Source attached to ${draft.taskId}`)
                    update({ surface: 'workspace', taskId: draft.taskId, captureId: null })
                    return capture
                  }
                  const created = await api.createTaskFromCapture(capture.id, {
                    title: draft.taskTitle!,
                    detail: draft.taskDetail ?? '',
                    priority: draft.priority,
                    due: draft.due,
                    objective_ids: draft.objectiveIds,
                  })
                  showNotice(`${created.id} created from ${draft.provider}`)
                  update({ surface: 'workspace', taskId: created.id, captureId: null })
                  return created
                } finally {
                  await Promise.all([
                    queryClient.invalidateQueries({ queryKey: ['captures'] }),
                    queryClient.invalidateQueries({ queryKey: ['workspace'] }),
                  ])
                }
              }}
              onConvert={(captureId, actionId) => runCaptureAction(() => api.convertCaptureAction(captureId, actionId), 'Action item converted to a task')}
              onDismiss={(captureId) => runCaptureAction(() => api.dismissCapture(captureId), 'Capture dismissed')}
              onCopyMicrosoftRequest={() => setMicrosoftOobMode('request')}
              onImportAgentResult={() => setMicrosoftOobMode('import')}
              onImport={() => setImportOpen(true)}
              onLink={(captureId, taskId) => runCaptureAction(() => api.linkCapture(captureId, taskId), `Context linked to ${taskId}`)}
              onSearchChange={(search) => update({ search }, { replace: true })}
              onSelectCapture={(captureId) => update({ captureId, taskId: null })}
              providerGates={providerGates}
              search={state.search}
              selectedCaptureId={state.captureId}
              workspace={workspace}
            />
          )}
        </main>
      </div>

      {workspace && state.taskId ? (
        <Suspense fallback={<aside aria-label={`Task ${state.taskId}`} className="detail-drawer"><LoadingBlock label="Opening Task…" /></aside>}>
          <TaskDrawer
            key={state.taskId}
            onClose={() => update({ taskId: null })}
            onNavigationLockChange={(locked) => { taskNavigationLockRef.current = locked }}
            onNotice={showNotice}
            onOpenObjective={(objectiveId) => update({ surface: 'objectives', objectiveId, captureId: null, taskId: null })}
            onOpenTask={(taskId) => update({ taskId, captureId: null })}
            providerGates={providerGates}
            taskId={state.taskId}
            workspace={workspace}
          />
        </Suspense>
      ) : selectedCapture && workspace ? (
        <CaptureDrawer
          capture={selectedCapture}
          onClose={() => update({ captureId: null })}
          onCreateTask={async (input) => {
            const created = await api.createTaskFromCapture(selectedCapture.id, input)
            await Promise.all([
              queryClient.invalidateQueries({ queryKey: ['captures'] }),
              queryClient.invalidateQueries({ queryKey: ['workspace'] }),
            ])
            showNotice(`${created.id} created from ${selectedCapture.id}`)
            update({ surface: 'workspace', taskId: created.id, captureId: null })
            return created
          }}
          providerGates={providerGates}
          workspace={workspace}
        />
      ) : null}

      <QuickTaskDialog
        error={quickTaskMutation.error ? getErrorMessage(quickTaskMutation.error) : null}
        initialObjectiveId={quickTaskInitialObjectiveId}
        onClose={() => { if (!quickTaskMutation.isPending) { setQuickTaskOpen(false); setQuickTaskInitialObjectiveId(''); quickTaskMutation.reset() } }}
        onSubmit={(input) => quickTaskMutation.mutate(input)}
        open={quickTaskOpen}
        pending={quickTaskMutation.isPending}
        resetDraftToken={quickTaskDraftResetToken}
        workspace={workspace}
      />
      <CommandPalette
        onClose={() => setCommandPaletteOpen(false)}
        onImportCapture={() => setImportOpen(true)}
        onNavigate={(target) => update({ ...target, captureId: null, taskId: null })}
        onNewTask={() => openQuickTask()}
        onOpenCapture={(captureId) => update({ surface: 'inbox', captureId, taskId: null })}
        onOpenObjective={(objectiveId) => update({ surface: 'objectives', objectiveId, captureId: null, taskId: null })}
        onOpenTask={(taskId) => update({ surface: 'workspace', captureId: null, taskId })}
        open={commandPaletteOpen}
        tasks={workspace?.tasks ?? []}
      />
      {workspace ? (
        <WorkspaceActionsDialog
          onClose={() => { if (!workspaceActionMutation.isPending) { setWorkspaceActionsOpen(false); workspaceActionMutation.reset() } }}
          onCreateNote={(text, links, idempotencyKey) => workspaceActionMutation.mutateAsync({ text, links, idempotencyKey }).then(() => undefined)}
          onNotice={showNotice}
          open={workspaceActionsOpen}
          pending={workspaceActionMutation.isPending}
          workspace={workspace}
        />
      ) : null}
      <CaptureImportDialog
        onClose={() => { if (!importMutation.isPending) { setImportOpen(false); importMutation.reset() } }}
        onSubmit={(packet) => importMutation.mutate(packet)}
        open={importOpen}
        pending={importMutation.isPending}
        serverError={importMutation.error ? getErrorMessage(importMutation.error) : null}
      />
      <MicrosoftOobDialog
        initialMode={microsoftOobMode ?? 'request'}
        onClose={() => { if (!oobImportMutation.isPending) { setMicrosoftOobMode(null); oobImportMutation.reset() } }}
        onSubmit={(packets) => oobImportMutation.mutate(packets)}
        open={microsoftOobMode !== null}
        pending={oobImportMutation.isPending}
        providerGates={providerGates}
        serverError={oobImportMutation.error ? getErrorMessage(oobImportMutation.error) : null}
      />

      {syncReviewOpen && syncStatus ? (
        <SyncStatusDialog
          adoptError={syncAdoptMutation.error ? getErrorMessage(syncAdoptMutation.error) : null}
          adopting={syncAdoptMutation.isPending}
          onAdopt={() => {
            if (!syncStatus.manifest_digest) return
            syncAdoptMutation.mutate({ generation: syncStatus.generation, manifestDigest: syncStatus.manifest_digest })
          }}
          onClose={() => setSyncReviewOpen(false)}
          onRefresh={refreshAuthoritativeState}
          status={syncStatus}
        />
      ) : null}

      {notice ? <div className={`toast toast--${notice.tone}`} role="status"><Icon name={notice.tone === 'success' ? 'check' : 'warning'} size={16} /><span>{notice.message}</span>{notice.action ? <button className="toast__action" onClick={notice.action.run} type="button">{notice.action.label}</button> : null}<button aria-label="Dismiss notification" onClick={() => setNotice(null)} type="button"><Icon name="close" size={14} /></button></div> : null}
    </div>
  )
}
