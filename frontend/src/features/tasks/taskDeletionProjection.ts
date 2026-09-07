import type { WorkspaceProjection } from '../../domain/types'

export function removeDeletedTaskFromWorkspace(
  workspace: WorkspaceProjection,
  taskId: string,
): WorkspaceProjection {
  return {
    ...workspace,
    edges: workspace.edges.filter((edge) => edge.source !== taskId && edge.target !== taskId),
    tasks: workspace.tasks.filter((item) => item.id !== taskId),
  }
}
