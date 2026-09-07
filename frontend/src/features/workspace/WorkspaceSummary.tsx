import { useMemo } from 'react'
import { Button, IconButton } from '../../components/Primitives'
import type { WorkspaceProjection } from '../../domain/types'
import { filterWorkspaceTasks } from './views/viewModels'

/**
 * The page heading and its workspace-level actions. The New task button also
 * carries the focus fallback marker that view-local surfaces hand focus back
 * to, so it must stay mounted for every workspace shape.
 */
export function WorkspacePageHeading({
  description,
  isRefreshing,
  onCreateTask,
  onRefresh,
}: {
  description: string
  isRefreshing: boolean
  onCreateTask: () => void
  onRefresh: () => void
}) {
  return (
    <header className="page-heading">
      <div>
        <div className="eyebrow"><span className="live-dot" /> Live workspace</div>
        <h1 id="workspace-heading">Keep execution connected to intent.</h1>
        <p>{description}. Every change stays local to this workspace.</p>
      </div>
      <div className="page-heading__actions">
        <IconButton
          disabled={isRefreshing}
          icon="refresh"
          label="Refresh workspace"
          onClick={onRefresh}
        />
        <Button
          data-workspace-focus-fallback=""
          icon="plus"
          onClick={onCreateTask}
          variant="primary"
        >New task</Button>
      </div>
    </header>
  )
}

/** Canonical workspace counts. These read the canonical Task set, never the
 * completed-visibility projection, so the strip stays a workspace fact. */
export function WorkspaceSummaryStrip({ workspace }: { workspace: WorkspaceProjection }) {
  const active = workspace.tasks.filter((task) => task.status === 'open' || task.status === 'started')
  const completed = workspace.tasks.filter((task) => task.status === 'done').length
  const critical = active.filter((task) => task.priority === 'P0').length
  const blocked = useMemo(
    () => filterWorkspaceTasks(workspace.tasks, { readiness: 'blocked' }).length,
    [workspace.tasks],
  )
  const aligned = workspace.tasks.filter((task) => task.objective_ids.length > 0).length
  return (
    <div className="metrics-strip" aria-label="Workspace summary">
      <div><span>Active</span><strong>{active.length}</strong><small>{workspace.tasks.filter((task) => task.status === 'started').length} in progress</small></div>
      <div><span>Completion</span><strong>{workspace.tasks.length ? Math.round((completed / workspace.tasks.length) * 100) : 0}%</strong><small>{completed} of {workspace.tasks.length} tasks</small></div>
      <div><span>Aligned</span><strong>{aligned}</strong><small>across {workspace.objectives.length} objectives</small></div>
      <div className={blocked ? 'metric-attention' : ''}><span>Blocked</span><strong>{blocked}</strong><small>{critical} P0 active tasks</small></div>
    </div>
  )
}
