declare module 'features/workspace/views' {
  import type { ComponentType, ReactNode } from 'react'
  import type { KeyResultProjection, OutcomeFilter } from '../features/workspace/views/keyResultModel'
  import type {
    Note,
    Objective,
    Task,
    TaskPriority,
    TaskStatus,
    WorkspaceEdge,
    WorkspaceView,
  } from '../domain/types'
  import type {
    CompletedVisibilityProjection,
  } from '../features/workspace/views/completedTaskVisibility'

  export interface WorkspaceViewsProps {
    view: WorkspaceView
    tasks: Task[]
    objectives: Objective[]
    notes: Note[]
    edges: WorkspaceEdge[]
    search: string
    status: 'all' | TaskStatus
    priority: 'all' | TaskPriority
    readiness: 'all' | 'ready' | 'blocked'
    timing: 'all' | 'overdue' | 'today' | 'soon' | 'unscheduled'
    today: string
    objectiveId: string
    selectedTaskId: string | null
    selectedObjectiveId?: string | null
    /** The single authoritative completed-visibility projection. */
    projection?: CompletedVisibilityProjection
    /** The single Page-owned Key Result projection; renderers never rebuild it. */
    /** The normalized scoped outcome coordinate, for selection display only. */
    outcome?: OutcomeFilter
    keyResultProjection?: KeyResultProjection
    /** Narrow callback: choosing an outcome only changes that filter coordinate. */
    onSelectOutcome?: (selection: { objectiveId: string; keyResultId: string }) => void
    /** Transient Graph interaction identities; never persisted anywhere. */
    contextTargetTaskId?: string | null
    focusPinnedTaskId?: string | null
    onContextTargetChange?: (taskId: string | null) => void
    onFocusPinChange?: (taskId: string | null) => void
    /** Page-owned prerequisite controls rendered inside the Graph context modal. */
    renderPopupPrerequisites?: (taskId: string) => ReactNode
    onSelectTask: (taskId: string) => void
    onSelectObjective: (objectiveId: string) => void
    onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  }

  export const WorkspaceViews: ComponentType<WorkspaceViewsProps>
}
