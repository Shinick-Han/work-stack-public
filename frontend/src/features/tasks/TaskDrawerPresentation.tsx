import { useEffect, useLayoutEffect, useRef } from 'react'
import { Icon } from '../../components/Icon'
import { EmptyState, IconButton, LoadingBlock } from '../../components/Primitives'
import type { MicrosoftProviderGates } from '../../config/providerGates'
import type { ApprovedReplyInput, ReplyCommand, ReplyReceipt, Task, TaskDetail, WorkspaceProjection } from '../../domain/types'
import { getErrorMessage } from '../../utils/format'
import { SnapshotExportDialog } from './SnapshotExportDialog'
import { TaskActionsDialog } from './TaskActionsDialog'
import { TaskContextSubview } from './TaskContextSubview'
import { TaskActivityTimeline } from './TaskDrawerTimelines'
import { TaskOverviewEditor } from './TaskOverviewEditor'
import { TaskOverviewSummary } from './TaskOverviewSummary'
import { TaskRelationshipsSection } from './TaskRelationshipsSection'
import { TaskReplySection } from './TaskReplySection'
import { escapeReturnsToTask } from './taskDrawerEscape'
import { TaskResumeFooter, TaskResumePanel, VIEW_ALL_CONTEXT_ID } from './TaskResumePanel'
import type { EditableTaskField, EditableTaskPatch } from './taskDrawerModel'
import type { TaskDrawerSelection } from './taskDrawerSelectors'
import { useTaskResumeFacts, type TaskResumeFactsResult } from './useTaskResumeFacts'
import './TaskResumePanel.css'

export type DrawerTab = 'resume' | 'activity' | 'details'
export type DrawerSurface = 'task' | 'context'
export type SaveState = 'idle' | 'saving' | 'saved' | 'error'

type DrawerNotice = (message: string, tone?: 'success' | 'error') => void

function saveStateLabel(saveState: SaveState) {
  if (saveState === 'saving') return 'Saving…'
  if (saveState === 'saved') return 'Saved'
  if (saveState === 'error') return 'Not saved'
  return ''
}

function draftContextCount(draft: Task | null) {
  return draft ? draft.context_count : 0
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

const DRAWER_TABS: readonly { id: DrawerTab; label: string }[] = [
  { id: 'resume', label: 'Resume' },
  { id: 'activity', label: 'Activity' },
  { id: 'details', label: 'Details' },
]

function tabElementId(tab: DrawerTab) {
  return `task-tab-${tab}`
}

function tabFromKey(key: string, current: DrawerTab): DrawerTab {
  const index = DRAWER_TABS.findIndex((item) => item.id === current)
  if (key === 'Home') return DRAWER_TABS[0].id
  if (key === 'End') return DRAWER_TABS[DRAWER_TABS.length - 1].id
  if (key === 'ArrowRight') return DRAWER_TABS[(index + 1) % DRAWER_TABS.length].id
  if (key === 'ArrowLeft') return DRAWER_TABS[(index - 1 + DRAWER_TABS.length) % DRAWER_TABS.length].id
  return current
}

function TaskDrawerTabs({ onTab, tab }: { onTab: (tab: DrawerTab) => void; tab: DrawerTab }) {
  return (
    <div
      aria-label="Task views"
      className="drawer-tabs task-drawer-tabs"
      onKeyDown={(event) => {
        const next = tabFromKey(event.key, tab)
        if (next === tab) return
        event.preventDefault()
        onTab(next)
        document.getElementById(tabElementId(next))?.focus()
      }}
      role="tablist"
    >
      {DRAWER_TABS.map((item) => (
        <button
          aria-controls={`task-panel-${item.id}`}
          aria-selected={tab === item.id}
          className={tab === item.id ? 'is-active' : ''}
          id={tabElementId(item.id)}
          key={item.id}
          onClick={() => onTab(item.id)}
          role="tab"
          tabIndex={tab === item.id ? 0 : -1}
          type="button"
        >{item.label}</button>
      ))}
    </div>
  )
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
  return <div className="drawer-overview" id="task-panel-details" role="tabpanel" aria-labelledby="task-tab-details">
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

function TaskDrawerActivityTab({
  active,
  activity,
  onCreate,
  onImportReceipt,
  onToggle,
  open,
  replies,
  selection,
  taskId,
}: {
  active: boolean
  activity: TaskDetail['activity']
  onCreate: (input: ApprovedReplyInput) => Promise<ReplyCommand>
  onImportReceipt: (replyId: string, receipt: ReplyReceipt) => Promise<ReplyCommand>
  onToggle: () => void
  open: boolean
  replies: ReplyCommand[]
  selection: TaskDrawerSelection
  taskId: string
}) {
  if (!active) return null
  return (
    <div id="task-panel-activity" role="tabpanel" aria-labelledby="task-tab-activity">
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
      <TaskActivityTimeline activity={activity} />
    </div>
  )
}

export function TaskDrawerDialogs({
  draft,
  onActionClose,
  onNotice,
  onPermanentlyDeleted,
  onSaved,
  onSnapshotClose,
  snapshotOpen,
  taskActionsOpen,
  taskId,
  workspaceUid,
}: {
  draft: Task | null
  onActionClose: () => void
  onNotice: DrawerNotice
  onPermanentlyDeleted: () => void
  onSaved: (task: Task) => void
  onSnapshotClose: () => void
  snapshotOpen: boolean
  taskActionsOpen: boolean
  taskId: string
  workspaceUid: string
}) {
  return <>
    <SnapshotExportDialog onClose={onSnapshotClose} onNotice={onNotice} open={snapshotOpen} taskId={taskId} />
    {draft ? (
      <TaskActionsDialog
        onClose={onActionClose}
        onNotice={onNotice}
        onPermanentlyDeleted={onPermanentlyDeleted}
        onSaved={onSaved}
        open={taskActionsOpen}
        task={draft}
        workspaceUid={workspaceUid}
      />
    ) : null}
  </>
}

export function drawerClassName(surface: DrawerSurface, tab: DrawerTab, ready: boolean) {
  if (surface === 'context') return 'detail-drawer detail-drawer--subview'
  return ready && tab === 'resume' ? 'detail-drawer detail-drawer--resume' : 'detail-drawer'
}

export interface TaskDrawerChromeProps {
  activity: TaskDetail['activity']
  canRetry: boolean
  context: TaskDetail['context']
  draft: Task | null
  loadError: unknown
  loadPending: boolean
  markDirty: (field: EditableTaskField) => void
  navigateAfterSave: (action: () => void) => void
  navigationLocked: boolean
  onActivityToggle: () => void
  onClose: () => void
  onContextBack: () => void
  onContextCreate: (input: ApprovedReplyInput) => Promise<ReplyCommand>
  onContextImport: (replyId: string, receipt: ReplyReceipt) => Promise<ReplyCommand>
  onDiscard: () => void
  onDraftChange: (task: Task) => void
  onInvalidTitle: () => void
  onMore: () => void
  onOpenContext: () => void
  onOpenObjective?: (objectiveId: string) => void
  onOpenSnapshot: () => void
  onOpenTask?: (taskId: string) => void
  onRecordProgress?: (taskId: string) => void
  onRetry: () => void
  onSave: (patch: EditableTaskPatch) => void
  onTab: (tab: DrawerTab) => void
  onTagTextChange: (value: string) => void
  progressLocked: boolean
  providerGates: MicrosoftProviderGates
  replies: ReplyCommand[]
  replyOpen: boolean
  saveError: string | null
  saveState: SaveState
  savedTask: Task | null
  selection: TaskDrawerSelection
  surface: DrawerSurface
  tab: DrawerTab
  tagText: string
  taskId: string
  workspace: WorkspaceProjection
}

interface TaskDrawerPanelsProps extends TaskDrawerChromeProps {
  resumeFacts: TaskResumeFactsResult
}

function TaskDrawerPanels(props: TaskDrawerPanelsProps) {
  const {
    activity, canRetry, context, draft, loadError, loadPending, markDirty, navigateAfterSave,
    navigationLocked, onActivityToggle, onContextBack, onContextCreate, onContextImport, onDiscard,
    onDraftChange, onInvalidTitle, onOpenContext, onOpenObjective, onOpenSnapshot, onOpenTask,
    onRetry, onSave, onTagTextChange, progressLocked, providerGates, replies, replyOpen, resumeFacts,
    saveError, saveState, savedTask, selection, surface, tab, tagText, taskId, workspace,
  } = props
  return (
    <div className="drawer-body">
      <TaskDrawerLoadState draft={draft} error={loadError} pending={loadPending} />
      {surface === 'context' && savedTask ? (
        <TaskContextSubview
          context={context}
          facts={resumeFacts}
          onBack={onContextBack}
          onCreate={onContextCreate}
          onImportReceipt={onContextImport}
          onToggle={onActivityToggle}
          open={replyOpen}
          providerGates={providerGates}
          replies={replies}
          selection={selection}
          task={savedTask}
          taskId={taskId}
          workspaceUid={workspace.workspace.id}
        />
      ) : null}
      {surface === 'task' && savedTask && tab === 'resume' ? (
        <div aria-labelledby="task-tab-resume" id="task-panel-resume" role="tabpanel">
          <TaskResumePanel
            contextCount={draftContextCount(draft)}
            facts={resumeFacts}
            objectives={selection.taskObjectives}
            onOpenContext={onOpenContext}
            onOpenObjective={onOpenObjective ? (objectiveId) => navigateAfterSave(() => onOpenObjective(objectiveId)) : undefined}
            progressLocked={progressLocked}
            savedTask={savedTask}
            workspaceUid={workspace.workspace.id}
          />
        </div>
      ) : null}
      <TaskDrawerOverviewTab
        active={surface === 'task' && tab === 'details'}
        canRetry={canRetry}
        draft={draft}
        markDirty={markDirty}
        navigateAfterSave={navigateAfterSave}
        navigationLocked={navigationLocked}
        onDiscard={onDiscard}
        onDraftChange={onDraftChange}
        onInvalidTitle={onInvalidTitle}
        onOpenObjective={onOpenObjective}
        onOpenSnapshot={onOpenSnapshot}
        onOpenTask={onOpenTask}
        onRetry={onRetry}
        onSave={onSave}
        onTagTextChange={onTagTextChange}
        saveError={saveError}
        saveState={saveState}
        selection={selection}
        tagText={tagText}
        workspace={workspace}
      />
      <TaskDrawerActivityTab
        active={surface === 'task' && tab === 'activity' && Boolean(draft)}
        activity={activity}
        onCreate={onContextCreate}
        onImportReceipt={onContextImport}
        onToggle={onActivityToggle}
        open={replyOpen}
        replies={replies}
        selection={selection}
        taskId={taskId}
      />
    </div>
  )
}

/**
 * Where the reader is put back down after the context subview closes.
 *
 * "View all context" is the control they left from, so it is the return. The
 * Resume tab is the fallback for the one case where the panel is not there to
 * return to — a Task whose saved projection has gone away underneath the
 * subview — because dropping focus on `<body>` is what this is here to prevent.
 */
function focusResumeReturn() {
  const target = document.getElementById(VIEW_ALL_CONTEXT_ID)
    ?? document.getElementById(tabElementId('resume'))
  target?.focus()
}

/**
 * Restores focus after React has actually committed `context -> task`.
 *
 * The button being focused does not exist while the handler that closed the
 * subview is still running: the surface change is state, and the Resume panel
 * mounts in the commit that follows. Reading the DOM in a layout effect keyed on
 * the transition is what makes the lookup find a mounted element, and comparing
 * against the previous surface is what keeps the first render — a drawer that
 * simply opened on Resume — from moving focus at all.
 */
function useResumeFocusReturn(surface: DrawerSurface) {
  const previous = useRef(surface)
  useLayoutEffect(() => {
    const returned = previous.current === 'context' && surface === 'task'
    previous.current = surface
    if (returned) focusResumeReturn()
  }, [surface])
}

/**
 * One window listener for the whole time the subview is open.
 *
 * The callback is read through a ref so a parent re-render does not tear down
 * and re-add the listener; the subscription depends on the surface alone.
 */
function useContextEscape(surface: DrawerSurface, onContextBack: () => void) {
  const back = useRef(onContextBack)
  useEffect(() => { back.current = onContextBack }, [onContextBack])
  useEffect(() => {
    if (surface !== 'context') return
    const onKey = (event: KeyboardEvent) => {
      if (!escapeReturnsToTask(event)) return
      event.preventDefault()
      back.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [surface])
}

export function TaskDrawerChrome(props: TaskDrawerChromeProps) {
  const { draft, navigationLocked, onClose, onContextBack, onMore, onRecordProgress, onTab, progressLocked, saveState, savedTask, surface, tab, taskId, workspace } = props
  const resumeFacts = useTaskResumeFacts(workspace.workspace.id, taskId)
  useContextEscape(surface, onContextBack)
  useResumeFocusReturn(surface)
  return (
    <>
      <TaskDrawerHeader navigationLocked={navigationLocked} onClose={onClose} onMore={onMore} saveState={saveState} taskId={taskId} />
      {surface === 'task' ? <TaskDrawerTabs onTab={onTab} tab={tab} /> : null}
      <TaskDrawerPanels {...props} resumeFacts={resumeFacts} />
      {draft && savedTask && surface === 'task' && tab === 'resume' ? (
        <TaskResumeFooter locked={progressLocked} onRecordProgress={onRecordProgress} taskId={taskId} />
      ) : null}
    </>
  )
}
