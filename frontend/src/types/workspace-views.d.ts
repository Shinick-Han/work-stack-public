declare module 'features/workspace/views' {
  import type { ComponentType } from 'react'
  import type {
    Note,
    Objective,
    Task,
    TaskPriority,
    TaskStatus,
    WorkspaceEdge,
    WorkspaceView,
  } from '../domain/types'

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
    onSelectTask: (taskId: string) => void
    onSelectObjective: (objectiveId: string) => void
    onChangeTaskStatus: (taskId: string, status: TaskStatus) => Promise<void>
  }

  export const WorkspaceViews: ComponentType<WorkspaceViewsProps>
}
