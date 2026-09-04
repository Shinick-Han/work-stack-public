import { lazy, Suspense, useEffect, useMemo, useRef, useState, type ComponentProps } from 'react'
import { useMutation, useQuery, useQueryClient, type QueryClient, type QueryKey } from '@tanstack/react-query'
import { api, ApiError, CommitUnknownError, createIdempotencyKey } from '../api/client'
import { BrandMark } from '../components/BrandMark'
import { Icon } from '../components/Icon'
import { Button, ErrorState, IconButton, LoadingBlock } from '../components/Primitives'
import { microsoftProviderGates, type MicrosoftProviderGates } from '../config/providerGates'
import { workstackConnectionCenterGates, type ConnectionCenterGates } from '../config/connectionCenterGates'
import type { AppUrlState, Capture, CapturePacket, CaptureTaskInput, QuickTaskInput, SyncStatus, Task, TaskDetail, TaskStatus, WorkspaceProjection } from '../domain/types'
import { CommandPalette } from '../features/commands/CommandPalette'
import { CaptureDrawer } from '../features/inbox/CaptureDrawer'
import { CaptureImportDialog } from '../features/inbox/CaptureImportDialog'
import { InboxPage } from '../features/inbox/InboxPage'
import { buildManualWebCapturePacket, type SourceCaptureDraft } from '../features/inbox/sourceCapture'
import { MicrosoftOobDialog, type MicrosoftOobMode } from '../features/integrations/MicrosoftOobDialog'
import { ObjectiveHubPage } from '../features/objectives/ObjectiveHubPage'
import { QuickTaskDialog } from '../features/tasks/QuickTaskDialog'
import { WorkspaceActionsDialog } from '../features/workspace/WorkspaceActionsDialog'
import { filterWorkspaceTasks } from '../features/workspace/views/viewModels'
import { getErrorMessage, getObjectiveTitle, statusLabels } from '../utils/format'
import { useUrlState } from './urlState'
import { subscribePlanningChanges } from '../integration/planningChangeBus'
import { SyncStatusControl, SyncStatusDialog, isSyncUnavailable, isSyncWriteBlocked } from './SyncStatusControl'
import { UpdateStatusControl } from './UpdateStatusControl'
import { subscribeSyncStatusEvents } from './syncStatusEvents'
import {
  useCheckpointNotices,
  type CheckpointNoticeIntent,
  useCheckpointTransitionNotices,
} from './useCheckpointNotices'
import {
  useTaskStatusIntent,
  type TaskStatusIntentResponse,
} from './useTaskStatusIntent'
import { applyTheme, readTheme, type WorkStackTheme } from './theme'
import { FocusSurface, ReviewSurface } from './AppSurfaces'
import { SsotConnectionCenter } from './SsotConnectionCenter'
import { MultiProfileConnectionCenter } from './MultiProfileConnectionCenter'
import { useWorkspaceRebind } from './useWorkspaceRebind'

const TaskDrawer = lazy(() => import('../features/tasks/TaskDrawer').then((module) => ({ default: module.TaskDrawer })))
const WorkspacePage = lazy(() => import('../features/workspace/WorkspacePage').then((module) => ({ default: module.WorkspacePage })))

interface Notice {
  action?: { label: string; run: () => void }
  id: number
  message: string
  tone: 'success' | 'error'
  /** The already-verified Task this notice may offer an explicit action for. */
  intent?: CheckpointNoticeIntent
}

interface AppProps {
  connectionCenterGates?: ConnectionCenterGates
  providerGates?: MicrosoftProviderGates
}

interface SyncAdoptOperation {
  coordinate: string
  key: string
}

function operationForCoordinate(current: SyncAdoptOperation, coordinate: string): SyncAdoptOperation {
  return current.coordinate === coordinate ? current : { coordinate, key: createIdempotencyKey() }
}

function syncCoordinateFor(status?: SyncStatus): string {
  return status ? `${status.generation}:${status.manifest_digest ?? ''}` : ''
}

function firstErrorMessage(primary: unknown, secondary: unknown): string | null {
  if (primary) return getErrorMessage(primary)
  if (secondary) return getErrorMessage(secondary)
  return null
}

function errorMessageOrNull(error: unknown): string | null {
  return error ? getErrorMessage(error) : null
}

function deriveSyncPresentation(syncError: unknown, syncStatus: SyncStatus | undefined, workspaceFailed: boolean) {
  const legacyEndpointUnavailable = syncError instanceof ApiError
    && syncError.status === 404
    && !syncStatus
    && !workspaceFailed
  const effectiveError = syncError && !legacyEndpointUnavailable ? syncError : null
  return {
    effectiveError,
    endpointDisconnected: Boolean(effectiveError && !isSyncUnavailable(effectiveError)),
    writeBlocked: isSyncWriteBlocked(syncStatus, effectiveError),
  }
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
    update({ ...numericTarget, captureId: null, taskId: null })
    return
  }
  if (!workspace || !['j', 'k'].includes(event.key.toLowerCase())) return
  const tasks = filteredShortcutTasks(workspace, state)
  if (!tasks.length || state.surface === 'inbox') return
  event.preventDefault()
  update({ captureId: null, taskId: adjacentTaskId(tasks, state.taskId, event.key) })
}

type UpdateUrl = (patch: Partial<AppUrlState>, options?: { replace?: boolean }) => boolean

interface SidebarTaskResults {
  total: number
  visible: WorkspaceProjection['tasks']
}

function SurfaceNavButton({ active, count, icon, label, onClick, shortcut, showCount = false }: {
  active: boolean
  count?: number
  icon: Parameters<typeof Icon>[0]['name']
  label: string
  onClick: () => void
  shortcut: string
  showCount?: boolean
}) {
  return <button aria-current={active ? 'page' : undefined} className={active ? 'is-active' : ''} onClick={onClick} type="button">
    <Icon name={icon} /><span>{label}</span>
    {showCount ? <span className="nav-shortcut">{count ? <b>{count}</b> : null}<kbd>{shortcut}</kbd></span> : <kbd>{shortcut}</kbd>}
  </button>
}

function PrimaryNavigation({ onNavigate, state, workspace }: { onNavigate: (surface: AppUrlState['surface']) => void; state: AppUrlState; workspace?: WorkspaceProjection }) {
  return <nav aria-label="Product surfaces" className="primary-nav">
    <SurfaceNavButton active={state.surface === 'workspace'} icon="graph" label="Workspace" onClick={() => onNavigate('workspace')} shortcut="1–4" />
    <SurfaceNavButton active={state.surface === 'focus'} icon="target" label="Focus" onClick={() => onNavigate('focus')} shortcut="5" />
    <SurfaceNavButton active={state.surface === 'inbox'} count={workspace?.inbox_count} icon="inbox" label="Context Inbox" onClick={() => onNavigate('inbox')} shortcut="6" showCount />
    <SurfaceNavButton active={state.surface === 'review'} icon="activity" label="Daily Review" onClick={() => onNavigate('review')} shortcut="7" />
    <SurfaceNavButton active={state.surface === 'objectives'} icon="target" label="Objective Hub" onClick={() => onNavigate('objectives')} shortcut="8" />
  </nav>
}

function SidebarObjectives({ closeMobile, state, update, workspace }: { closeMobile: () => void; state: AppUrlState; update: UpdateUrl; workspace?: WorkspaceProjection }) {
  const objectives = workspace ? workspace.objectives : []
  const tasks = workspace ? workspace.tasks : []
  return <details className="sidebar-section sidebar-objectives">
    <summary role="button"><span>Objectives</span><span>{objectives.length}</span><Icon name="chevronDown" size={14} /></summary>
    <div className="sidebar-section-body">
      <button className="sidebar-view-all" onClick={() => update({ surface: 'objectives', objectiveId: 'all' })} type="button">Open Objective Hub</button>
      <div className="objective-nav">
        {objectives.map((objective, index) => {
          const count = tasks.filter((task) => task.objective_ids.includes(objective.id) && task.status !== 'done' && task.status !== 'dropped').length
          return <button
            className={state.surface === 'objectives' && state.objectiveId === objective.id ? 'is-active' : ''}
            key={objective.id}
            onClick={() => { update({ surface: 'objectives', objectiveId: objective.id, captureId: null }); closeMobile() }}
            type="button"
          >
            <span className={`objective-dot objective-dot--${(index % 5) + 1}`} />
            <span><strong>{objective.id}</strong><small>{getObjectiveTitle(objective)}</small></span>
            <em>{count}</em>
          </button>
        })}
      </div>
    </div>
  </details>
}

function SidebarTasks({ closeMobile, query, results, setQuery, state, update, workspace }: {
  closeMobile: () => void
  query: string
  results: SidebarTaskResults
  setQuery: (query: string) => void
  state: AppUrlState
  update: UpdateUrl
  workspace?: WorkspaceProjection
}) {
  return <details className="sidebar-section sidebar-tasks">
    <summary role="button"><span>Tasks</span><span>{workspace ? workspace.tasks.length : 0}</span><Icon name="chevronDown" size={14} /></summary>
    <div className="sidebar-section-body">
      <button className="sidebar-view-all" onClick={() => { update({ surface: 'workspace', view: 'table', captureId: null, taskId: null }); closeMobile() }} type="button">Open Table</button>
      <label className="sidebar-task-search">
        <span className="sr-only">Filter sidebar tasks</span><Icon name="search" size={13} />
        <input maxLength={120} onChange={(event) => setQuery(event.target.value)} placeholder="Find a Task…" type="search" value={query} />
      </label>
      <div className="task-nav">
        {results.visible.map((task) => <button
          aria-label={`Open task ${task.id}: ${task.title}`}
          className={state.taskId === task.id ? 'is-active' : ''}
          key={task.id}
          onClick={() => { update({ surface: 'workspace', captureId: null, taskId: task.id }); closeMobile() }}
          type="button"
        >
          <span className={`task-priority-dot task-priority-dot--${task.priority.toLowerCase()}`} />
          <span><strong>{task.id}</strong><small>{task.title}</small></span>
          <em>{task.priority} · {statusLabels[task.status]}</em>
        </button>)}
        {!results.total ? <p className="sidebar-task-empty">No Tasks match.</p> : null}
      </div>
      {results.total > results.visible.length ? <p className="sidebar-task-limit">Showing first 50 of {results.total}</p> : null}
    </div>
  </details>
}

function SidebarConnectionEntry({ detail, gates, label, onOpen }: { detail: string; gates: ConnectionCenterGates; label: string; onOpen: () => void }) {
  if (!gates.registry) return <SsotConnectionCenter fallbackDetail={detail} fallbackLabel={label} />
  return <button aria-label="Configure SSOT connections" className="ssot-connection-control" onClick={onOpen} type="button">
    <span className="ssot-connection-control__dot" /><span><strong>{label}</strong><small>{detail}</small></span>
  </button>
}

function AppSidebar({ connectionCenterGates, mobileNavOpen, onOpenConnectionCenter, setMobileNavOpen, setSidebarTaskQuery, sidebarTaskQuery, sidebarTaskResults, state, syncEndpointDisconnected, syncStatus, update, workspace }: {
  connectionCenterGates: ConnectionCenterGates
  mobileNavOpen: boolean
  onOpenConnectionCenter: () => void
  setMobileNavOpen: (open: boolean) => void
  setSidebarTaskQuery: (query: string) => void
  sidebarTaskQuery: string
  sidebarTaskResults: SidebarTaskResults
  state: AppUrlState
  syncEndpointDisconnected: boolean
  syncStatus?: SyncStatus
  update: UpdateUrl
  workspace?: WorkspaceProjection
}) {
  const closeMobile = () => setMobileNavOpen(false)
  const navigate = (surface: AppUrlState['surface']) => {
    update(surface === 'inbox' ? { surface, taskId: null } : { surface, captureId: null })
    closeMobile()
  }
  const ssotAvailable = Boolean(syncStatus || syncEndpointDisconnected)
  const fallbackDetail = syncStatus
    ? `Generation ${syncStatus.generation} · ${syncStatus.state}`
    : syncEndpointDisconnected ? 'Connection unavailable · writes blocked' : 'On this device · no background sync'
  return <>
    <aside className={`app-sidebar ${mobileNavOpen ? 'is-open' : ''}`}>
      <div className="brand-block">
        <BrandMark />
        <div><strong>Work Stack</strong><small>Aligned execution</small></div>
        <IconButton className="mobile-close" icon="close" label="Close navigation" onClick={closeMobile} variant="ghost" />
      </div>
      <PrimaryNavigation onNavigate={navigate} state={state} workspace={workspace} />
      <SidebarObjectives closeMobile={closeMobile} state={state} update={update} workspace={workspace} />
      <SidebarTasks closeMobile={closeMobile} query={sidebarTaskQuery} results={sidebarTaskResults} setQuery={setSidebarTaskQuery} state={state} update={update} workspace={workspace} />
      <SidebarConnectionEntry detail={fallbackDetail} gates={connectionCenterGates} label={ssotAvailable ? 'SSOT workspace' : 'Local workspace'} onOpen={onOpenConnectionCenter} />
    </aside>
    {mobileNavOpen ? <button aria-label="Close navigation" className="sidebar-scrim" onClick={closeMobile} type="button" /> : null}
  </>
}

function surfaceTitle(surface: AppUrlState['surface']) {
  const titles: Record<AppUrlState['surface'], string> = {
    workspace: 'Workspace', focus: 'Focus', review: 'Daily Review', objectives: 'Objective Hub', inbox: 'Context Inbox',
  }
  return titles[surface]
}

function AppTopbar({ beginSyncReview, effectiveSyncError, isSyncFetching, onCommand, onConfigureSsot, onMore, onRefresh, onToggleTheme, onMenu, state, syncStatus, theme, workspace }: {
  beginSyncReview: () => void
  effectiveSyncError: unknown
  isSyncFetching: boolean
  onCommand: () => void
  onConfigureSsot?: () => void
  onMenu: () => void
  onMore: () => void
  onRefresh: () => void
  onToggleTheme: () => void
  state: AppUrlState
  syncStatus?: SyncStatus
  theme: WorkStackTheme
  workspace?: WorkspaceProjection
}) {
  return <header className="app-topbar">
    <div className="topbar-left">
      <IconButton className="mobile-menu" icon="menu" label="Open navigation" onClick={onMenu} variant="ghost" />
      <span>{workspace ? workspace.workspace.name : 'Work Stack'}</span><span>/</span><strong>{surfaceTitle(state.surface)}</strong>
    </div>
    <div className="topbar-actions">
      <UpdateStatusControl />
      <SyncStatusControl error={effectiveSyncError} isFetching={isSyncFetching} onConfigureSsot={onConfigureSsot} onRefresh={onRefresh} onReview={beginSyncReview} status={syncStatus} />
      <button className="command-button" onClick={onCommand} type="button"><Icon name="command" size={15} /> Search or jump <kbd>⌘ K</kbd></button>
      <IconButton icon={theme === 'dark' ? 'sun' : 'moon'} label={`Use ${theme === 'dark' ? 'light' : 'dark'} theme`} onClick={onToggleTheme} variant="ghost" />
      <IconButton icon="more" label="More workspace actions" onClick={onMore} variant="ghost" />
    </div>
  </header>
}

function WorkspaceRefreshWarning({ error, isFetching, onRetry }: { error: unknown; isFetching: boolean; onRetry: () => void }) {
  return <div aria-label="Workspace refresh failed" className="workspace-refresh-warning" role="status">
    <div><strong>Showing the last confirmed workspace</strong><span>{getErrorMessage(error)}</span></div>
    <Button disabled={isFetching} onClick={onRetry} variant="secondary">{isFetching ? 'Retrying…' : 'Retry workspace refresh'}</Button>
  </div>
}

interface AppMainProps {
  captures: Capture[]
  capturesError: unknown
  capturesFailed: boolean
  capturesPending: boolean
  onCaptureAction: (operation: () => Promise<unknown>, successMessage: string) => Promise<void>
  onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  onCreateSourceTask: (draft: SourceCaptureDraft) => Promise<unknown>
  onCreateTask: (objectiveId?: string) => void
  onImport: () => void
  onImportAgentResult: () => void
  onMicrosoftRequest: () => void
  onNotice: (message: string, tone?: Notice['tone'], action?: Notice['action']) => void
  onRefetchCaptures: () => void
  onRefetchWorkspace: () => void
  providerGates: MicrosoftProviderGates
  state: AppUrlState
  update: UpdateUrl
  workspace?: WorkspaceProjection
  workspaceError: unknown
  workspaceFailed: boolean
  workspacePending: boolean
  workspaceRefreshing: boolean
  workspaceRecoveryRequired: boolean
}

function InboxSurface(props: AppMainProps & { workspace: WorkspaceProjection }) {
  if (props.capturesPending) return <LoadingBlock label="Loading sanitized context…" />
  if (props.capturesFailed) return <ErrorState message={getErrorMessage(props.capturesError)} onRetry={props.onRefetchCaptures} />
  return <InboxPage
    captures={props.captures}
    onConvert={(captureId, actionId) => props.onCaptureAction(() => api.convertCaptureAction(captureId, actionId), 'Action item converted to a task')}
    onCreateSourceTask={props.onCreateSourceTask}
    onDismiss={(captureId) => props.onCaptureAction(() => api.dismissCapture(captureId), 'Capture dismissed')}
    onCopyMicrosoftRequest={props.onMicrosoftRequest}
    onImport={props.onImport}
    onImportAgentResult={props.onImportAgentResult}
    onLink={(captureId, taskId) => props.onCaptureAction(() => api.linkCapture(captureId, taskId), `Context linked to ${taskId}`)}
    onSearchChange={(search) => props.update({ search }, { replace: true })}
    onSelectCapture={(captureId) => props.update({ captureId, taskId: null })}
    providerGates={props.providerGates}
    search={props.state.search}
    selectedCaptureId={props.state.captureId}
    workspace={props.workspace}
  />
}

function LoadedAppSurface(props: AppMainProps & { workspace: WorkspaceProjection }) {
  switch (props.state.surface) {
    case 'workspace':
      return <Suspense fallback={<LoadingBlock label="Opening workspace view…" />}>
        <WorkspacePage
          isRefreshing={props.workspaceRefreshing}
          onChangeTaskStatus={props.onChangeTaskStatus}
          onCreateTask={() => props.onCreateTask()}
          onOpenObjectives={() => props.update({ surface: 'objectives', objectiveId: 'all', taskId: null, captureId: null })}
          onRefresh={props.onRefetchWorkspace}
          state={props.state}
          updateUrl={props.update}
          workspace={props.workspace}
        />
      </Suspense>
    case 'focus':
      return <FocusSurface
        isRefreshing={props.workspaceRefreshing}
        onChangeTaskStatus={props.onChangeTaskStatus}
        onCreateTask={() => props.onCreateTask()}
        onNotice={props.onNotice}
        onRefresh={props.onRefetchWorkspace}
        onSelectTask={(taskId) => props.update({ taskId, captureId: null })}
        workspace={props.workspace}
      />
    case 'review':
      return <ReviewSurface
        captures={props.captures}
        onNotice={props.onNotice}
        onOpenCapture={(captureId) => props.update({ surface: 'inbox', captureId, taskId: null })}
        onOpenTask={(taskId) => props.update({ taskId, captureId: null })}
        workspace={props.workspace}
      />
    case 'objectives':
      return <ObjectiveHubPage
        objectiveId={props.state.objectiveId}
        onCreateAlignedTask={props.onCreateTask}
        onNotice={props.onNotice}
        onOpenTask={(taskId) => props.update({ taskId, captureId: null })}
        onSelectObjective={(objectiveId) => props.update({ objectiveId })}
        workspace={props.workspace}
      />
    case 'inbox':
      return <InboxSurface {...props} />
  }
}

function AppMain(props: AppMainProps & { writeBlocked: boolean }) {
  const content = props.workspacePending
    ? <LoadingBlock label="Opening your workspace…" />
    : props.workspace
      ? <div
          aria-disabled={props.writeBlocked || undefined}
          className="app-main__write-surface"
          inert={props.writeBlocked || undefined}
        >
          <LoadedAppSurface {...props} workspace={props.workspace} />
        </div>
      : <ErrorState
          message={getErrorMessage(props.workspaceError)}
          onRetry={props.workspaceRecoveryRequired ? undefined : props.onRefetchWorkspace}
        />
  return <main className="app-main">
    {props.workspace && props.workspaceFailed ? <WorkspaceRefreshWarning error={props.workspaceError} isFetching={props.workspaceRefreshing} onRetry={props.onRefetchWorkspace} /> : null}
    {content}
  </main>
}

function AppDrawerLayer({ capture, onCreateCaptureTask, onNotice, providerGates, state, taskNavigationLockRef, update, workspace }: {
  capture: Capture | null
  onCreateCaptureTask: (input: CaptureTaskInput) => Promise<Task>
  onNotice: (message: string, tone?: Notice['tone'], action?: Notice['action']) => void
  providerGates: MicrosoftProviderGates
  state: AppUrlState
  taskNavigationLockRef: { current: boolean }
  update: UpdateUrl
  workspace?: WorkspaceProjection
}) {
  if (!workspace) return null
  if (state.taskId) return <Suspense fallback={<aside aria-label={`Task ${state.taskId}`} className="detail-drawer"><LoadingBlock label="Opening Task…" /></aside>}>
    <TaskDrawer
      key={state.taskId}
      onClose={() => update({ taskId: null })}
      onNavigationLockChange={(locked) => { taskNavigationLockRef.current = locked }}
      onNotice={onNotice}
      onOpenObjective={(objectiveId) => update({ surface: 'objectives', objectiveId, captureId: null, taskId: null })}
      onOpenTask={(taskId) => update({ taskId, captureId: null })}
      providerGates={providerGates}
      taskId={state.taskId}
      workspace={workspace}
    />
  </Suspense>
  if (!capture) return null
  return <CaptureDrawer capture={capture} onClose={() => update({ captureId: null })} onCreateTask={onCreateCaptureTask} providerGates={providerGates} workspace={workspace} />
}

interface AppDialogLayerProps {
  captureImport: ComponentProps<typeof CaptureImportDialog>
  commandPalette: ComponentProps<typeof CommandPalette>
  microsoftOob: ComponentProps<typeof MicrosoftOobDialog>
  quickTask: ComponentProps<typeof QuickTaskDialog>
  syncStatus?: ComponentProps<typeof SyncStatusDialog>
  workspaceActions?: ComponentProps<typeof WorkspaceActionsDialog>
}

function AppDialogLayer({ captureImport, commandPalette, microsoftOob, quickTask, syncStatus, workspaceActions }: AppDialogLayerProps) {
  return <>
    <QuickTaskDialog {...quickTask} />
    <CommandPalette {...commandPalette} />
    {workspaceActions ? <WorkspaceActionsDialog {...workspaceActions} /> : null}
    <CaptureImportDialog {...captureImport} />
    <MicrosoftOobDialog {...microsoftOob} />
    {syncStatus ? <SyncStatusDialog {...syncStatus} /> : null}
  </>
}

/**
 * The approved bounded affordance for the keyed status intent. View and the
 * explicit forward action coexist; a pending or ambiguous intent keeps its
 * frozen key and offers Retry instead of silently starting a new one.
 */
function AppNoticeIntentAction({ intent }: { intent: TaskStatusIntentAffordance | null }) {
  if (!intent) return null
  if (intent.pending) return <span className="toast__action" role="status">Marking in progress…</span>
  if (intent.retryable) {
    return <button className="toast__action" onClick={intent.retry} type="button">Retry mark in progress</button>
  }
  if (intent.undoAvailable) {
    return <button className="toast__action" onClick={intent.undo} type="button">Undo mark in progress</button>
  }
  if (!intent.canMarkInProgress) return null
  return <button className="toast__action" onClick={intent.markInProgress} type="button">Mark In Progress</button>
}

function AppNotice({ intent, notice, onDismiss }: {
  intent: TaskStatusIntentAffordance | null
  notice: Notice | null
  onDismiss: () => void
}) {
  if (!notice) return null
  return <div className={`toast toast--${notice.tone}`} role="status">
    <Icon name={notice.tone === 'success' ? 'check' : 'warning'} size={16} />
    <span>{notice.message}</span>
    {notice.action ? <button className="toast__action" onClick={notice.action.run} type="button">{notice.action.label}</button> : null}
    <AppNoticeIntentAction intent={intent} />
    <button aria-label="Dismiss notification" onClick={onDismiss} type="button"><Icon name="close" size={14} /></button>
  </div>
}

const NO_INTENT_OWNER = {
  ownerEpoch: 'checkpoint-intent:none',
  taskId: '',
  taskUid: '',
  workspaceUid: '',
}

/**
 * GI-F1: the LIVE authority for one intent. The rendered snapshot is never the
 * input: status, revision, detail identity and write authority are read from
 * the cache at the moment the hook asks.
 */
function liveIntentAuthority(
  client: QueryClient,
  intent: CheckpointNoticeIntent | null,
) {
  if (!intent) return { detailAgrees: false, task: null, writable: false }
  const live = client.getQueryData<WorkspaceProjection>(['workspace'])
    ?.tasks.find((item) => item.id === intent.taskId)
  const detail = client.getQueryData<TaskDetail>(['task', intent.taskId])
  const task = live && live.uid === intent.taskUid ? live : null
  // A cached detail for this Task must agree; an absent detail is not a defect.
  const detailAgrees = !detail || (detail.task.id === intent.taskId && detail.task.uid === intent.taskUid)
  const sync = client.getQueryState<SyncStatus>(['sync-status'])
  const workspaceFailed = client.getQueryState(['workspace'])?.status === 'error'
  const { writeBlocked } = deriveSyncPresentation(sync?.error, sync?.data, workspaceFailed)
  return { detailAgrees, task, writable: !writeBlocked }
}

/** Every explicit dispatch and every completion re-reads that authority. */
function isIntentAuthorityCurrent(
  client: QueryClient,
  intent: CheckpointNoticeIntent | null,
  ownerEpoch: string,
) {
  if (!intent || intent.ownerEpoch !== ownerEpoch || !intent.isCurrent()) return false
  const authority = liveIntentAuthority(client, intent)
  return !!authority.task && authority.detailAgrees && authority.writable
}

interface ReconcileChronology {
  revisionFloor: number
  unchanged: () => boolean
}

/**
 * GI-F2: query identity and update chronology are captured BEFORE the reads, so
 * a newer arrival during the fetch survives instead of being overwritten.
 */
function captureReconcileChronology(
  client: QueryClient,
  intent: CheckpointNoticeIntent,
): ReconcileChronology {
  const cache = client.getQueryCache()
  const keys: QueryKey[] = [['workspace'], ['task', intent.taskId]]
  const snapshots = keys.map((key) => {
    const query = cache.find({ exact: true, queryKey: key })
    return { data: query?.state.data, key, query, updates: query?.state.dataUpdateCount }
  })
  const cachedTask = client.getQueryData<WorkspaceProjection>(['workspace'])
    ?.tasks.find((item) => item.id === intent.taskId)
  const cachedDetail = client.getQueryData<TaskDetail>(['task', intent.taskId])
  return {
    // An inactive detail can already be newer than the workspace when reads begin.
    revisionFloor: Math.max(cachedTask?.revision ?? 0, cachedDetail?.task.revision ?? 0),
    unchanged: () => snapshots.every((snapshot) => {
      const live = cache.find({ exact: true, queryKey: snapshot.key })
      // Removal, recreation, a newer update count or a replaced value all refuse.
      return live === snapshot.query
        && live?.state.dataUpdateCount === snapshot.updates
        && live?.state.data === snapshot.data
    }),
  }
}

/** The fetched pair must answer the captured owner and may not roll back. */
function acceptsReconciledPair(
  before: ReconcileChronology,
  intent: CheckpointNoticeIntent,
  workspace: WorkspaceProjection,
  detail: TaskDetail,
) {
  if (workspace.workspace.id !== intent.workspaceUid) return false
  const live = workspace.tasks.find((item) => item.id === intent.taskId)
  if (!live || live.uid !== intent.taskUid) return false
  if (detail.task.id !== intent.taskId || detail.task.uid !== intent.taskUid) return false
  if (detail.task.revision !== live.revision) return false
  return live.revision >= before.revisionFloor
}

/** The capability a notice carries, if any. */
function noticeIntentOf(notice: Notice | null) {
  return notice?.intent ?? null
}

/**
 * GI-F4: an unresolved intent keeps its own owner. Presentation may be replaced
 * underneath it; ownership may not.
 */
function currentIntentOwnerOf(owned: CheckpointNoticeIntent | null, notice: Notice | null) {
  const presented = noticeIntentOf(notice)
  if (!owned || !owned.isCurrent()) return presented
  if (presented && !sameIntentTask(owned, presented)) return presented
  return owned
}

function sameIntentTask(left: CheckpointNoticeIntent, right: CheckpointNoticeIntent) {
  return left.workspaceUid === right.workspaceUid
    && left.taskId === right.taskId
    && left.taskUid === right.taskUid
}

function retainedIntentOwner(
  intent: CheckpointNoticeIntent | null,
  state: ReturnType<typeof useTaskStatusIntent>,
) {
  return holdsNotice(state) ? intent : null
}

/** A pending or ambiguous intent holds its notice, and therefore its key. */
function holdsNotice(state: ReturnType<typeof useTaskStatusIntent>) {
  return state.pending || state.retryable
}

/** Liveness for a captured owner: same lifetime AND same live Task identity. */
function isIntentOwnerCurrent(intent: CheckpointNoticeIntent | null, ownerEpoch: string) {
  if (!intent || intent.ownerEpoch !== ownerEpoch) return false
  return intent.isCurrent()
}

function intentOwnerOf(intent: CheckpointNoticeIntent | null) {
  if (!intent) return NO_INTENT_OWNER
  const { ownerEpoch, taskId, taskUid, workspaceUid } = intent
  return { ownerEpoch, taskId, taskUid, workspaceUid }
}

/** The authoritative current Task for this intent, or null when it is gone. */
function liveIntentTask(workspace: WorkspaceProjection | undefined, intent: CheckpointNoticeIntent | null) {
  if (!intent) return null
  const live = workspace?.tasks.find((item) => item.id === intent.taskId)
  return live && live.uid === intent.taskUid ? live : null
}

function intentAffordanceOf(
  intent: CheckpointNoticeIntent | null,
  state: ReturnType<typeof useTaskStatusIntent>,
): TaskStatusIntentAffordance | null {
  if (!intent) return null
  return {
    canMarkInProgress: state.canMarkInProgress,
    markInProgress: state.markInProgress,
    pending: state.pending,
    retry: state.retry,
    retryable: state.retryable,
    undo: state.undoLast,
    undoAvailable: !!state.undoOffer,
  }
}

interface TaskStatusIntentAffordance {
  canMarkInProgress: boolean
  markInProgress: () => void
  pending: boolean
  retry: () => void
  retryable: boolean
  undo: () => void
  undoAvailable: boolean
}

function connectionCenterAction(gates: ConnectionCenterGates, onOpen: () => void): (() => void) | undefined {
  return gates.registry ? onOpen : undefined
}

function synchronizationReviewAction(status: SyncStatus | undefined, onOpen: () => void): (() => void) | undefined {
  return status ? onOpen : undefined
}

function appShellClass(drawerOpen: boolean, syncWriteBlocked: boolean): string {
  let className = 'app-shell'
  if (drawerOpen) className += ' has-drawer'
  if (syncWriteBlocked) className += ' is-write-blocked'
  return className
}

function useAutoOpenWorkspaceRebind({
  coordinate,
  onOpen,
  rebindAvailable,
  workspaceError,
  workspaceLoaded,
}: {
  coordinate: string
  onOpen: () => void
  rebindAvailable?: boolean
  workspaceError: boolean
  workspaceLoaded: boolean
}) {
  const openedCoordinateRef = useRef('')
  useEffect(() => {
    if (!workspaceError || workspaceLoaded || !rebindAvailable || !coordinate) return
    if (openedCoordinateRef.current === coordinate) return
    openedCoordinateRef.current = coordinate
    onOpen()
  }, [coordinate, onOpen, rebindAvailable, workspaceError, workspaceLoaded])
}

export function App({ connectionCenterGates = workstackConnectionCenterGates, providerGates = microsoftProviderGates }: AppProps) {
  const queryClient = useQueryClient()
  const pendingStatusMutations = useRef(0)
  const taskNavigationLockRef = useRef(false)
  const syncAdoptOperationRef = useRef({ coordinate: '', key: createIdempotencyKey() })
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
  const noticeHoldRef = useRef(false)
  const ownedIntentRef = useRef<CheckpointNoticeIntent | null>(null)
  const [syncReviewOpen, setSyncReviewOpen] = useState(false)
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
    mutationFn: ({ generation, idempotencyKey, manifestDigest }: { generation: number; idempotencyKey: string; manifestDigest: string }) => (
      api.adoptSyncChanges(generation, manifestDigest, idempotencyKey)
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

  const workspaceRebind = useWorkspaceRebind({
    open: syncReviewOpen,
    status: syncStatusQuery.data,
    onRecovered: (result) => {
      setSyncReviewOpen(false)
      showNotice(`Workspace reconnected · receipt ${result.recovery_receipt_digest.slice(7, 19)}`)
    },
  })
  const workspaceRebindError = firstErrorMessage(
    workspaceRebind.mutation.error,
    workspaceRebind.previewQuery.error,
  )

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

  const receiveCheckpoint = useCheckpointNotices({
    workspace: workspaceQuery.data,
    onNotice: ({ intent, message, view }) => showNotice(message, 'success', { label: 'View', run: view }, intent),
    onView: (event) => { update({ surface: 'workspace', taskId: event.task_id, captureId: null }) },
  })
  // The transition notice is a separate strict variant on the SAME stream.
  const receiveTransition = useCheckpointTransitionNotices({
    workspace: workspaceQuery.data,
    onNotice: ({ message, view }) => showNotice(message, 'success', { label: 'View', run: view }),
    onView: (event) => { update({ surface: 'review', taskId: null, captureId: null }) },
  })
  useEffect(
    () => subscribeSyncStatusEvents(refreshAuthoritativeState, receiveCheckpoint, receiveTransition),
    [queryClient, receiveCheckpoint, receiveTransition],
  )

  // The ONLY status-intent state machine: the admitted hook, with its frozen
  // options. No second hook, transport, mailbox or status setter is introduced.
  // GI-F4: presentation is replaceable, ownership of an UNRESOLVED intent is
  // not. While one is pending or ambiguous its owner survives a new
  // informational notice and the ordinary presentation timeout, so the frozen
  // key can never be replaced by a fresh Mark.
  const noticeIntent = currentIntentOwnerOf(ownedIntentRef.current, notice)
  const intentOwner = useMemo(() => intentOwnerOf(noticeIntent), [noticeIntent])

  // The live capability the getter and the guards read at call time.
  const noticeIntentRef = useRef(noticeIntent)
  noticeIntentRef.current = noticeIntent

  const statusIntent = useTaskStatusIntent({
    // GI-F1: a getter, so the accepted hook reads the LIVE authoritative Task at
    // its own dispatch and receipt decision points instead of a render snapshot.
    // The hook's options contract is unchanged and nothing is synthesized.
    get currentTask() {
      return liveIntentAuthority(queryClient, noticeIntentRef.current).task ?? null
    },
    // Liveness is answered from the cache at call time, not from this render.
    isCurrent: (owner) => isIntentAuthorityCurrent(
      queryClient, noticeIntentRef.current, owner.ownerEpoch,
    ),
    mutateOnce: (intent) => api.patchTaskStatusIntent(
      intent.taskId,
      { revision: intent.body.revision, status: intent.body.status },
      intent.key,
    ) as Promise<TaskStatusIntentResponse>,
    newKey: () => createIdempotencyKey(),
    owner: intentOwner,
    reconcile: async () => {
      // GI-F2: display-only reconciliation. BOTH authoritative snapshots are
      // fetched, all chronology is captured BEFORE the reads, and the whole
      // validated pair installs together or not at all.
      const captured = noticeIntentRef.current
      if (!captured) return
      const before = captureReconcileChronology(queryClient, captured)
      const [workspace, detail] = await Promise.all([
        api.getWorkspace(),
        api.getTask(captured.taskId),
      ])
      if (noticeIntentRef.current !== captured || !before.unchanged()) return
      if (!acceptsReconciledPair(before, captured, workspace, detail)) return
      if (!isIntentAuthorityCurrent(queryClient, captured, captured.ownerEpoch)) return
      queryClient.setQueryData(['workspace'], workspace)
      queryClient.setQueryData(['task', captured.taskId], detail)
    },
  })
  // Holding is a fact about the CURRENT intent, read by the dismissal timer.
  noticeHoldRef.current = holdsNotice(statusIntent)
  // An unresolved intent keeps its owner; a settled one releases it.
  ownedIntentRef.current = retainedIntentOwner(noticeIntent, statusIntent)
  const noticeHoldToken = `${statusIntent.pending}:${statusIntent.retryable}`
  useEffect(() => {
    if (!notice) return
    // An in-flight or ambiguous keyed intent keeps its notice, and therefore its
    // frozen key, until it settles: the timeout must not manufacture a new one.
    if (noticeHoldRef.current) return
    const timer = window.setTimeout(() => setNotice((current) => current?.id === notice.id ? null : current), 3200)
    return () => window.clearTimeout(timer)
  }, [notice, noticeHoldToken])

  const intentAffordance = intentAffordanceOf(noticeIntent, statusIntent)

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
  const syncPresentation = deriveSyncPresentation(syncStatusQuery.error, syncStatus, workspaceQuery.isError)
  const effectiveSyncError = syncPresentation.effectiveError
  const syncEndpointDisconnected = syncPresentation.endpointDisconnected
  const syncWriteBlocked = syncPresentation.writeBlocked
  const syncCoordinate = syncCoordinateFor(syncStatus)
  const beginSyncReview = () => {
    syncAdoptOperationRef.current = { coordinate: syncCoordinate, key: createIdempotencyKey() }
    syncAdoptMutation.reset()
    workspaceRebind.begin()
    setSyncReviewOpen(true)
  }
  const refreshSyncReview = () => {
    syncAdoptOperationRef.current = { coordinate: '', key: createIdempotencyKey() }
    syncAdoptMutation.reset()
    workspaceRebind.refresh()
    refreshAuthoritativeState()
  }
  const adoptReviewedSync = () => {
    if (!syncStatus?.manifest_digest) return
    syncAdoptOperationRef.current = operationForCoordinate(syncAdoptOperationRef.current, syncCoordinate)
    syncAdoptMutation.mutate({
      generation: syncStatus.generation,
      idempotencyKey: syncAdoptOperationRef.current.key,
      manifestDigest: syncStatus.manifest_digest,
    })
  }
  useAutoOpenWorkspaceRebind({
    coordinate: syncCoordinate,
    onOpen: beginSyncReview,
    rebindAvailable: syncStatus?.rebind_available,
    workspaceError: workspaceQuery.isError,
    workspaceLoaded: workspaceQuery.data !== undefined,
  })
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
    onNavigate: (target) => update({ ...target, captureId: null, taskId: null }),
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
