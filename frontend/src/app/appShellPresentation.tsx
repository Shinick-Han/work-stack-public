import { lazy, Suspense, type ComponentProps } from 'react'
import { api } from '../api/client'
import { Icon } from '../components/Icon'
import { Button, ErrorState, IconButton, LoadingBlock } from '../components/Primitives'
import type { ConnectionCenterGates } from '../config/connectionCenterGates'
import type { MicrosoftProviderGates } from '../config/providerGates'
import type { AppUrlState, Capture, CaptureTaskInput, SyncStatus, Task, TaskStatus, WorkspaceProjection } from '../domain/types'
import { CommandPalette } from '../features/commands/CommandPalette'
import { CaptureDrawer } from '../features/inbox/CaptureDrawer'
import { CaptureImportDialog } from '../features/inbox/CaptureImportDialog'
import type { SourceCaptureDraft } from '../features/inbox/sourceCapture'
import { InboxPage } from '../features/inbox/InboxPage'
import type { KnowledgeImportEnvelope } from '../domain/knowledgeImport'
import { MicrosoftOobDialog } from '../features/integrations/MicrosoftOobDialog'
import { ObjectiveHubPage } from '../features/objectives/ObjectiveHubPage'
import { QuickTaskDialog } from '../features/tasks/QuickTaskDialog'
import { WorkspaceActionsDialog } from '../features/workspace/WorkspaceActionsDialog'
import { getErrorMessage } from '../utils/format'
import { FocusSurface, ReviewSurface } from './AppSurfaces'
import { SyncStatusControl, SyncStatusDialog } from './SyncStatusControl'
import { UpdateStatusControl } from './UpdateStatusControl'
import type { UpdateUrl } from './appSidebarModel'
import type { WorkStackTheme } from './theme'
import type { CheckpointNoticeIntent } from './useCheckpointNotices'

const TaskDrawer = lazy(() => import('../features/tasks/TaskDrawer').then((module) => ({ default: module.TaskDrawer })))
const WorkspacePage = lazy(() => import('../features/workspace/WorkspacePage').then((module) => ({ default: module.WorkspacePage })))

export interface Notice {
  action?: { label: string; run: () => void }
  id: number
  message: string
  tone: 'success' | 'error'
  /** The already-verified Task this notice may offer an explicit action for. */
  intent?: CheckpointNoticeIntent
}

export interface TaskStatusIntentAffordance {
  canMarkInProgress: boolean
  markInProgress: () => void
  pending: boolean
  retry: () => void
  retryable: boolean
  undo: () => void
  undoAvailable: boolean
}

export function surfaceTitle(surface: AppUrlState['surface']) {
  const titles: Record<AppUrlState['surface'], string> = {
    workspace: 'Workspace', focus: 'Focus', review: 'Daily Review', objectives: 'Objective Hub', inbox: 'Context Inbox',
  }
  return titles[surface]
}

export function appShellClass(drawerOpen: boolean, syncWriteBlocked: boolean): string {
  let className = 'app-shell'
  if (drawerOpen) className += ' has-drawer'
  if (syncWriteBlocked) className += ' is-write-blocked'
  return className
}

export function connectionCenterAction(gates: ConnectionCenterGates, onOpen: () => void): (() => void) | undefined {
  return gates.registry ? onOpen : undefined
}

export function synchronizationReviewAction(status: SyncStatus | undefined, onOpen: () => void): (() => void) | undefined {
  return status ? onOpen : undefined
}

export function AppTopbar({ beginSyncReview, effectiveSyncError, isSyncFetching, onCommand, onConfigureSsot, onMore, onRefresh, onToggleTheme, onMenu, state, syncStatus, theme, workspace }: {
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

export interface AppMainProps {
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
  onReviewKnowledge?: (envelope: KnowledgeImportEnvelope) => void
  onReviewNavigationLockChange?: (locked: boolean) => void
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
    onReviewKnowledge={props.onReviewKnowledge}
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
        initialTaskId={props.state.reviewTaskId}
        onNavigationLockChange={props.onReviewNavigationLockChange}
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

export function AppMain(props: AppMainProps & { writeBlocked: boolean }) {
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

export function AppDrawerLayer({ capture, onCreateCaptureTask, onNotice, onReviewKnowledge, providerGates, state, taskNavigationLockRef, update, workspace }: {
  capture: Capture | null
  onCreateCaptureTask: (input: CaptureTaskInput) => Promise<Task>
  onNotice: (message: string, tone?: Notice['tone'], action?: Notice['action']) => void
  onReviewKnowledge?: (envelope: KnowledgeImportEnvelope) => void
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
      onRecordProgress={(taskId) => update({ surface: 'review', reviewTaskId: taskId, taskId: null, captureId: null })}
      onOpenObjective={(objectiveId) => update({ surface: 'objectives', objectiveId, captureId: null, taskId: null })}
      onOpenTask={(taskId) => update({ taskId, captureId: null })}
      providerGates={providerGates}
      taskId={state.taskId}
      workspace={workspace}
    />
  </Suspense>
  if (!capture) return null
  return <CaptureDrawer capture={capture} onClose={() => update({ captureId: null })} onCreateTask={onCreateCaptureTask} onReviewKnowledge={onReviewKnowledge} providerGates={providerGates} workspace={workspace} />
}

export interface AppDialogLayerProps {
  captureImport: ComponentProps<typeof CaptureImportDialog>
  commandPalette: ComponentProps<typeof CommandPalette>
  microsoftOob: ComponentProps<typeof MicrosoftOobDialog>
  quickTask: ComponentProps<typeof QuickTaskDialog>
  syncStatus?: ComponentProps<typeof SyncStatusDialog>
  workspaceActions?: ComponentProps<typeof WorkspaceActionsDialog>
}

export function AppDialogLayer({ captureImport, commandPalette, microsoftOob, quickTask, syncStatus, workspaceActions }: AppDialogLayerProps) {
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

export function AppNotice({ intent, notice, onDismiss }: {
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
