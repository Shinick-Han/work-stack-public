import { useId, useMemo, useState } from 'react'
import { BrandMark } from '../components/BrandMark'
import { Icon } from '../components/Icon'
import { IconButton } from '../components/Primitives'
import type { ConnectionCenterGates } from '../config/connectionCenterGates'
import type { AppUrlState, Objective, SyncStatus, Task, WorkspaceProjection } from '../domain/types'
import { getObjectiveTitle, statusLabels } from '../utils/format'
import { SsotConnectionCenter } from './SsotConnectionCenter'
import {
  groupSidebarTasks,
  isOpenTask,
  sharedObjectiveSuffix,
  unassignedGroupKey,
  type SidebarTaskResults,
  type UpdateUrl,
} from './appSidebarModel'

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

/**
 * One source for a sidebar row's selected state, so a row can never carry the is-active styling
 * without also announcing itself. aria-current="page" matches what the surface navigation already
 * exposes, which keeps one selection vocabulary across the whole sidebar.
 */
function navSelection(selected: boolean, className = '') {
  return {
    'aria-current': selected ? ('page' as const) : undefined,
    className: selected ? `${className} is-active`.trim() : className,
  }
}

function SidebarObjectiveTask({ closeMobile, groupLabel, sharedCount, state, task, update }: {
  closeMobile: () => void
  groupLabel: string
  sharedCount: number
  state: AppUrlState
  task: Task
  update: UpdateUrl
}) {
  return <li>
    <button
      aria-label={`Open task ${task.id}: ${task.title} under ${groupLabel}${sharedObjectiveSuffix(sharedCount)}`}
      {...navSelection(state.taskId === task.id, 'objective-nav__task')}
      onClick={() => { update({ surface: 'workspace', captureId: null, taskId: task.id }); closeMobile() }}
      type="button"
    >
      <span className={`task-priority-dot task-priority-dot--${task.priority.toLowerCase()}`} />
      <span className="objective-nav__text"><strong>{task.id}</strong><small>{task.title}</small></span>
      {sharedCount > 1 ? <em>{sharedCount} Objectives</em> : null}
    </button>
  </li>
}

function SidebarObjectiveGroup({ closeMobile, index, linked, listId, objective, onToggle, open, sharedCountOf, state, update }: {
  closeMobile: () => void
  index: number
  linked: readonly Task[]
  listId: string
  objective: Objective
  onToggle: () => void
  open: boolean
  sharedCountOf: (taskId: string) => number
  state: AppUrlState
  update: UpdateUrl
}) {
  const listLabel = `Tasks linked to ${objective.id}`
  return <li className="objective-nav__group">
    <div className="objective-nav__row">
      <button
        aria-controls={listId}
        aria-expanded={open}
        aria-label={listLabel}
        className="objective-nav__disclosure"
        onClick={onToggle}
        type="button"
      ><Icon name="chevronDown" size={12} /></button>
      <button
        {...navSelection(state.surface === 'objectives' && state.objectiveId === objective.id, 'objective-nav__select')}
        onClick={() => { update({ surface: 'objectives', objectiveId: objective.id, captureId: null }); closeMobile() }}
        type="button"
      >
        <span className={`objective-dot objective-dot--${(index % 5) + 1}`} />
        <span className="objective-nav__text"><strong>{objective.id}</strong><small>{getObjectiveTitle(objective)}</small></span>
        <em>{linked.filter(isOpenTask).length}</em>
      </button>
    </div>
    <ul aria-label={listLabel} className="objective-nav__tasks" hidden={!open} id={listId}>
      {linked.length ? linked.map((task) => <SidebarObjectiveTask
        closeMobile={closeMobile}
        groupLabel={objective.id}
        key={task.id}
        sharedCount={sharedCountOf(task.id)}
        state={state}
        task={task}
        update={update}
      />) : <li className="objective-nav__empty">No Tasks linked to {objective.id}.</li>}
    </ul>
  </li>
}

function SidebarUnassignedGroup({ closeMobile, listId, onToggle, open, state, tasks, update }: {
  closeMobile: () => void
  listId: string
  onToggle: () => void
  open: boolean
  state: AppUrlState
  tasks: readonly Task[]
  update: UpdateUrl
}) {
  return <li className="objective-nav__group">
    <button
      aria-controls={listId}
      aria-expanded={open}
      aria-label="Tasks with no Objective"
      className="objective-nav__select objective-nav__select--unassigned"
      onClick={onToggle}
      type="button"
    >
      <Icon name="chevronDown" size={12} />
      <span className="objective-dot objective-dot--unassigned" />
      <span className="objective-nav__text"><strong>Unassigned</strong><small>No Objective link</small></span>
      <em>{tasks.filter(isOpenTask).length}</em>
    </button>
    <ul aria-label="Tasks with no Objective" className="objective-nav__tasks" hidden={!open} id={listId}>
      {tasks.map((task) => <SidebarObjectiveTask
        closeMobile={closeMobile}
        groupLabel="no Objective"
        key={task.id}
        sharedCount={0}
        state={state}
        task={task}
        update={update}
      />)}
    </ul>
  </li>
}

function SidebarObjectives({ closeMobile, state, update, workspace }: { closeMobile: () => void; state: AppUrlState; update: UpdateUrl; workspace?: WorkspaceProjection }) {
  const listPrefix = useId()
  const [expandedOverrides, setExpandedOverrides] = useState<Record<string, boolean>>({})
  const objectives = workspace ? workspace.objectives : []
  const tasks = workspace ? workspace.tasks : []
  const groups = useMemo(() => groupSidebarTasks(objectives, tasks), [objectives, tasks])
  // The selected Objective reveals its Tasks by default; an explicit toggle wins and survives every
  // later selection change, so a collapse the operator asked for is never undone underneath them.
  const defaultExpanded = (key: string) => state.surface === 'objectives' && state.objectiveId === key
  const expanded = (key: string) => expandedOverrides[key] ?? defaultExpanded(key)
  const toggle = (key: string) => setExpandedOverrides((current) => ({
    ...current,
    [key]: !(current[key] ?? defaultExpanded(key)),
  }))
  const unassignedListId = `${listPrefix}-unassigned`
  return <details className="sidebar-section sidebar-objectives">
    <summary role="button"><span>Objectives</span><span>{objectives.length}</span><Icon name="chevronDown" size={14} /></summary>
    <div className="sidebar-section-body">
      <button className="sidebar-view-all" onClick={() => update({ surface: 'objectives', objectiveId: 'all' })} type="button">Open Objective Hub</button>
      <ul className="objective-nav">
        {objectives.map((objective, index) => <SidebarObjectiveGroup
          closeMobile={closeMobile}
          index={index}
          key={objective.id}
          linked={groups.byObjective.get(objective.id) ?? []}
          listId={`${listPrefix}-${objective.id}`}
          objective={objective}
          onToggle={() => toggle(objective.id)}
          open={expanded(objective.id)}
          sharedCountOf={(taskId) => groups.objectiveCounts.get(taskId) ?? 1}
          state={state}
          update={update}
        />)}
        {groups.unassigned.length ? <SidebarUnassignedGroup
          closeMobile={closeMobile}
          listId={unassignedListId}
          onToggle={() => toggle(unassignedGroupKey)}
          open={expanded(unassignedGroupKey)}
          state={state}
          tasks={groups.unassigned}
          update={update}
        /> : null}
      </ul>
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
          {...navSelection(state.taskId === task.id)}
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

export function AppSidebar({ connectionCenterGates, mobileNavOpen, onOpenConnectionCenter, setMobileNavOpen, setSidebarTaskQuery, sidebarTaskQuery, sidebarTaskResults, state, syncEndpointDisconnected, syncStatus, update, workspace }: {
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
