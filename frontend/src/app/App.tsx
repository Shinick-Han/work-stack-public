import { useEffect, useMemo, useRef, useState, type ComponentProps } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, CommitUnknownError, createIdempotencyKey } from '../api/client'
import { microsoftProviderGates, type MicrosoftProviderGates } from '../config/providerGates'
import { workstackConnectionCenterGates, type ConnectionCenterGates } from '../config/connectionCenterGates'
import type { AppUrlState, CapturePacket, CaptureTaskInput, QuickTaskInput, Task, TaskStatus, WorkspaceProjection } from '../domain/types'
import { CommandPalette } from '../features/commands/CommandPalette'
import { CaptureImportDialog } from '../features/inbox/CaptureImportDialog'
import { buildManualWebCapturePacket, type SourceCaptureDraft } from '../features/inbox/sourceCapture'
import { MicrosoftOobDialog, type MicrosoftOobMode } from '../features/integrations/MicrosoftOobDialog'
import { QuickTaskDialog } from '../features/tasks/QuickTaskDialog'
import { WorkspaceActionsDialog } from '../features/workspace/WorkspaceActionsDialog'
import { filterWorkspaceTasks } from '../features/workspace/views/viewModels'
import { getErrorMessage } from '../utils/format'
import { useUrlState } from './urlState'
import { allowDraftNavigation, reviewNavigationPatch } from './reviewNavigation'
import { subscribePlanningChanges } from '../integration/planningChangeBus'
import { SyncStatusDialog } from './SyncStatusControl'
import { applyTheme, readTheme, type WorkStackTheme } from './theme'
import { MultiProfileConnectionCenter } from './MultiProfileConnectionCenter'
import { AppSidebar } from './AppSidebar'
import { projectSidebarTaskResults } from './appSidebarModel'
import type { CheckpointNoticeIntent } from './useCheckpointNotices'
import {
  AppDialogLayer,
  AppDrawerLayer,
  AppMain,
  AppNotice,
  AppTopbar,
  appShellClass,
  connectionCenterAction,
  synchronizationReviewAction,
  type Notice,
} from './appShellPresentation'
import { useAppShellNotice } from './useAppShellNotice'
import {
  refreshAuthoritativeQueries,
  useAppShellSync,
  useAppShellWriteBlock,
  useAutoOpenWorkspaceRebind,
} from './useAppShellSync'

interface AppProps {
  connectionCenterGates?: ConnectionCenterGates
  providerGates?: MicrosoftProviderGates
}

function errorMessageOrNull(error: unknown): string | null {
  return error ? getErrorMessage(error) : null
}

const numericShortcutTargets: Record<string, Partial<AppUrlState>> = {
  '1': { surface: 'workspace', view: 'graph' },
  '2': { surface: 'workspace', view: 'board' },
  '3': { surface: 'workspace', view: 'treemap' },
  '4': { surface: 'workspace', view: 'table' },
  '5': { surface: 'focus' },
  '6': { surface: 'inbox' },
  '7': { surface: 'review' },
  '8': { surface: 'objectives' },
}

function workspaceShortcutBlocked(event: KeyboardEvent) {
  const target = event.target
  const editable = target instanceof HTMLElement
    && (target.isContentEditable || target.matches('input, textarea, select'))
  return event.defaultPrevented
    || event.altKey
    || event.ctrlKey
    || event.metaKey
    || event.shiftKey
    || editable
    || Boolean(document.querySelector('dialog[open]'))
}

function filteredShortcutTasks(workspace: WorkspaceProjection, state: AppUrlState) {
  return filterWorkspaceTasks(workspace.tasks, {
    search: state.search,
    status: state.status,
    priority: state.priority,
    objectiveId: state.objectiveId,
    // The helper builds a NEW filter object, so an omitted field is not
    // forwarded by passing the whole state: the coordinate is explicit here.
    outcome: state.outcomeFilter,
  })
}

function adjacentTaskId(tasks: readonly { id: string }[], currentTaskId: string | null, key: string) {
  const currentIndex = tasks.findIndex((task) => task.id === currentTaskId)
  const direction = key.toLowerCase() === 'j' ? 1 : -1
  if (currentIndex < 0) return direction === 1 ? tasks[0].id : tasks[tasks.length - 1].id
  return tasks[(currentIndex + direction + tasks.length) % tasks.length].id
}

function handleWorkspaceShortcut(event: KeyboardEvent, state: AppUrlState, workspace: WorkspaceProjection | undefined, update: (patch: Partial<AppUrlState>) => boolean) {
  if (workspaceShortcutBlocked(event)) return
  const numericTarget = numericShortcutTargets[event.key]
  if (numericTarget) {
    event.preventDefault()
    update(numericTarget.surface === 'review' ? reviewNavigationPatch(state) : { ...numericTarget, captureId: null, taskId: null })
    return
  }
  if (!workspace || !['j', 'k'].includes(event.key.toLowerCase())) return
  const tasks = filteredShortcutTasks(workspace, state)
  if (!tasks.length || state.surface === 'inbox') return
  event.preventDefault()
  update({ captureId: null, taskId: adjacentTaskId(tasks, state.taskId, event.key) })
}

export function App({ connectionCenterGates = workstackConnectionCenterGates, providerGates = microsoftProviderGates }: AppProps) {
  const queryClient = useQueryClient()
  const pendingStatusMutations = useRef(0)
  const taskNavigationLockRef = useRef(false)
  const reviewNavigationLockRef = useRef(false)
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
  const [theme, setTheme] = useState<WorkStackTheme>(readTheme)
  const [connectionCenterOpen, setConnectionCenterOpen] = useState(false)

  const openQuickTask = (objectiveId = '') => {
    setQuickTaskInitialObjectiveId(objectiveId)
    setQuickTaskOpen(true)
  }

  const showNotice = (
    message: string,
    tone: Notice['tone'] = 'success',
    action?: Notice['action'],
    intent?: CheckpointNoticeIntent,
  ) => {
    setNotice({ action, id: Date.now(), intent, message, tone })
  }

  const { state, update } = useUrlState((current, next) =>
    allowDraftNavigation(current, next, taskNavigationLockRef.current, reviewNavigationLockRef.current, showNotice))

  useEffect(() => {
    applyTheme(theme)
  }, [theme])


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

  const sync = useAppShellSync({
    queryClient,
    showNotice,
    workspaceFailed: workspaceQuery.isError,
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

  const refreshAuthoritativeState = () => refreshAuthoritativeQueries(queryClient)
  const intentAffordance = useAppShellNotice({
    notice,
    queryClient,
    refreshAuthoritativeState,
    setNotice,
    showNotice,
    update,
    workspace: workspaceQuery.data,
  })

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
  const {
    adoptReviewedSync,
    beginSyncReview,
    effectiveError: effectiveSyncError,
    endpointDisconnected: syncEndpointDisconnected,
    refreshSyncReview,
    setSyncReviewOpen,
    syncAdoptMutation,
    syncCoordinate,
    syncReviewOpen,
    syncStatus,
    syncStatusQuery,
    workspaceRebind,
    workspaceRebindError,
    writeBlocked: syncWriteBlocked,
  } = sync
  useAutoOpenWorkspaceRebind({
    coordinate: syncCoordinate,
    onOpen: beginSyncReview,
    rebindAvailable: syncStatus?.rebind_available,
    workspaceError: workspaceQuery.isError,
    workspaceLoaded: workspaceQuery.data !== undefined,
  })
  useAppShellWriteBlock({
    selectedCaptureId: selectedCapture?.id,
    setImportOpen,
    setMicrosoftOobMode,
    setQuickTaskOpen,
    setWorkspaceActionsOpen,
    syncWriteBlocked,
    taskId: state.taskId,
  })
  const sidebarTaskResults = useMemo(
    () => projectSidebarTaskResults(sidebarTaskQuery, workspace?.tasks),
    [sidebarTaskQuery, workspace?.tasks],
  )

  useEffect(() => {
    const listener = (event: KeyboardEvent) => handleWorkspaceShortcut(event, state, workspace, update)
    window.addEventListener('keydown', listener)
    return () => window.removeEventListener('keydown', listener)
  }, [state.objectiveId, state.outcomeFilter, state.priority, state.search, state.status, state.surface, state.taskId, update, workspace])

  const createSourceTask = async (draft: SourceCaptureDraft): Promise<unknown> => {
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
        intent_id: draft.intentId,
      }, `workstack:${draft.intentId}`)
      showNotice(`${created.id} created from ${draft.provider}`)
      update({ surface: 'workspace', taskId: created.id, captureId: null })
      return created
    } finally {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['captures'] }),
        queryClient.invalidateQueries({ queryKey: ['workspace'] }),
      ])
    }
  }

  const createCaptureTask = async (input: CaptureTaskInput): Promise<Task> => {
    if (!selectedCapture) throw new Error('The selected capture is no longer available.')
    const created = await api.createTaskFromCapture(selectedCapture.id, input)
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['captures'] }),
      queryClient.invalidateQueries({ queryKey: ['workspace'] }),
    ])
    showNotice(`${created.id} created from ${selectedCapture.id}`)
    update({ surface: 'workspace', taskId: created.id, captureId: null })
    return created
  }

  const quickTaskDialogProps: ComponentProps<typeof QuickTaskDialog> = {
    error: errorMessageOrNull(quickTaskMutation.error),
    initialObjectiveId: quickTaskInitialObjectiveId,
    onClose: () => {
      if (quickTaskMutation.isPending) return
      setQuickTaskOpen(false)
      setQuickTaskInitialObjectiveId('')
      quickTaskMutation.reset()
    },
    onSubmit: (input) => quickTaskMutation.mutate(input),
    open: quickTaskOpen,
    pending: quickTaskMutation.isPending,
    resetDraftToken: quickTaskDraftResetToken,
    workspace,
  }
  const commandPaletteProps: ComponentProps<typeof CommandPalette> = {
    onClose: () => setCommandPaletteOpen(false),
    onImportCapture: () => setImportOpen(true),
    onNavigate: (target) => update(target.surface === 'review' ? reviewNavigationPatch(state) : { ...target, captureId: null, taskId: null }),
    onNewTask: () => openQuickTask(),
    onOpenCapture: (captureId) => update({ surface: 'inbox', captureId, taskId: null }),
    onOpenObjective: (objectiveId) => update({ surface: 'objectives', objectiveId, captureId: null, taskId: null }),
    onOpenTask: (taskId) => update({ surface: 'workspace', captureId: null, taskId }),
    open: commandPaletteOpen,
    tasks: workspace?.tasks ?? [],
  }
  const workspaceActionsProps: ComponentProps<typeof WorkspaceActionsDialog> | undefined = workspace ? {
    onClose: () => {
      if (workspaceActionMutation.isPending) return
      setWorkspaceActionsOpen(false)
      workspaceActionMutation.reset()
    },
    onCreateNote: (text, links, idempotencyKey) => workspaceActionMutation.mutateAsync({ text, links, idempotencyKey }).then(() => undefined),
    onNotice: showNotice,
    open: workspaceActionsOpen,
    pending: workspaceActionMutation.isPending,
    workspace,
  } : undefined
  const captureImportProps: ComponentProps<typeof CaptureImportDialog> = {
    onClose: () => {
      if (importMutation.isPending) return
      setImportOpen(false)
      importMutation.reset()
    },
    onSubmit: (packet) => importMutation.mutate(packet),
    open: importOpen,
    pending: importMutation.isPending,
    serverError: errorMessageOrNull(importMutation.error),
  }
  const microsoftOobProps: ComponentProps<typeof MicrosoftOobDialog> = {
    initialMode: microsoftOobMode ?? 'request',
    onClose: () => {
      if (oobImportMutation.isPending) return
      setMicrosoftOobMode(null)
      oobImportMutation.reset()
    },
    onSubmit: (packets) => oobImportMutation.mutate(packets),
    open: microsoftOobMode !== null,
    pending: oobImportMutation.isPending,
    providerGates,
    serverError: errorMessageOrNull(oobImportMutation.error),
  }
  const syncStatusProps: ComponentProps<typeof SyncStatusDialog> | undefined = syncReviewOpen && syncStatus ? {
    adoptError: errorMessageOrNull(syncAdoptMutation.error),
    adopting: syncAdoptMutation.isPending,
    onAdopt: adoptReviewedSync,
    onClose: () => setSyncReviewOpen(false),
    onRebind: workspaceRebind.run,
    onRefresh: refreshSyncReview,
    rebindError: workspaceRebindError,
    rebindPreview: workspaceRebind.previewQuery.data,
    rebinding: workspaceRebind.mutation.isPending,
    refreshing: syncStatusQuery.isFetching,
    status: syncStatus,
  } : undefined

  return (
    <div className={appShellClass(drawerOpen, syncWriteBlocked)}>
      <AppSidebar connectionCenterGates={connectionCenterGates} mobileNavOpen={mobileNavOpen} onOpenConnectionCenter={() => setConnectionCenterOpen(true)} setMobileNavOpen={setMobileNavOpen} setSidebarTaskQuery={setSidebarTaskQuery} sidebarTaskQuery={sidebarTaskQuery} sidebarTaskResults={sidebarTaskResults} state={state} syncEndpointDisconnected={syncEndpointDisconnected} syncStatus={syncStatus} update={update} workspace={workspace} />

      <div className="app-stage">
        <AppTopbar beginSyncReview={beginSyncReview} effectiveSyncError={effectiveSyncError} isSyncFetching={syncStatusQuery.isFetching} onCommand={() => setCommandPaletteOpen(true)} onConfigureSsot={connectionCenterAction(connectionCenterGates, () => setConnectionCenterOpen(true))} onMenu={() => setMobileNavOpen(true)} onMore={() => setWorkspaceActionsOpen(true)} onRefresh={refreshAuthoritativeState} onToggleTheme={() => setTheme((current) => current === 'dark' ? 'light' : 'dark')} state={state} syncStatus={syncStatus} theme={theme} workspace={workspace} />

        <AppMain
          captures={capturesQuery.data ?? []}
          capturesError={capturesQuery.error}
          capturesFailed={capturesQuery.isError}
          capturesPending={capturesQuery.isPending}
          onCaptureAction={runCaptureAction}
          onChangeTaskStatus={changeTaskStatus}
          onCreateSourceTask={createSourceTask}
          onCreateTask={openQuickTask}
          onImport={() => setImportOpen(true)}
          onImportAgentResult={() => setMicrosoftOobMode('import')}
          onMicrosoftRequest={() => setMicrosoftOobMode('request')}
          onNotice={showNotice}
          onRefetchCaptures={() => void capturesQuery.refetch()}
          onRefetchWorkspace={() => void workspaceQuery.refetch()}
          onReviewNavigationLockChange={(locked) => { reviewNavigationLockRef.current = locked }}
          providerGates={providerGates}
          state={state}
          update={update}
          workspace={workspace}
          workspaceError={workspaceQuery.error}
          workspaceFailed={workspaceQuery.isError}
          workspacePending={workspaceQuery.isPending}
          workspaceRefreshing={workspaceQuery.isFetching}
          workspaceRecoveryRequired={Boolean(syncStatus?.rebind_available)}
          writeBlocked={syncWriteBlocked}
        />
      </div>

      <AppDrawerLayer
        capture={selectedCapture}
        onCreateCaptureTask={createCaptureTask}
        onNotice={showNotice}
        providerGates={providerGates}
        state={state}
        taskNavigationLockRef={taskNavigationLockRef}
        update={update}
        workspace={workspace}
      />

      <AppDialogLayer
        captureImport={captureImportProps}
        commandPalette={commandPaletteProps}
        microsoftOob={microsoftOobProps}
        quickTask={quickTaskDialogProps}
        syncStatus={syncStatusProps}
        workspaceActions={workspaceActionsProps}
      />
      <MultiProfileConnectionCenter
        activationEnabled={connectionCenterGates.activation}
        enabled={connectionCenterGates.registry}
        onClose={() => setConnectionCenterOpen(false)}
        onReviewSynchronization={synchronizationReviewAction(syncStatus, () => {
          setConnectionCenterOpen(false)
          beginSyncReview()
        })}
        open={connectionCenterOpen}
      />
      <AppNotice intent={intentAffordance} notice={notice} onDismiss={() => setNotice(null)} />
    </div>
  )
}
