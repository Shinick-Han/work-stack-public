import type { Objective, Task } from './types'
import {
  projectKeyResults,
  type KeyResultCounts,
  type KeyResultProjection,
  type KeyResultProjectionInput,
} from './keyResultModel'

/**
 * Pure Objective → Key Result → Task hierarchy (plan §1.3 / §6 K1 / §8.4).
 *
 * No React, DOM, clock, transport, or global mutable state. Does not repair
 * stored KR refs and does not auto-align parent Objectives (that write
 * boundary is K2).
 *
 * Legacy `unassignedTaskIds` (copied from `projectKeyResults`) still means
 * "no KR refs", which includes Objective-only Tasks. New callers should use
 * `unaligned.taskIds` (floating) and `objectiveOnlyTaskIds`.
 */

export const UNASSIGNED_TASK_IDS_COMPAT =
  'legacy unassignedTaskIds = zero KR refs, including Objective-only; unaligned.taskIds = floating only'

export interface OutcomeHierarchyTaskRef {
  taskId: string
}

export interface OutcomeHierarchyKeyResultNode {
  key: string
  objectiveId: string
  keyResultId: string
  recordedProgress: number | null
  linkedTaskIds: string[]
  visibleTaskIds: string[]
  counts: KeyResultCounts
  visibleCounts: KeyResultCounts
  taskRefs: OutcomeHierarchyTaskRef[]
}

export interface OutcomeHierarchyObjectiveNode {
  objectiveId: string
  keyResults: OutcomeHierarchyKeyResultNode[]
  objectiveOnlyTaskIds: string[]
}

export interface OutcomeHierarchy {
  workspaceId: string
  objectiveNodes: OutcomeHierarchyObjectiveNode[]
  unaligned: { taskIds: string[] }
  unresolved: { taskIds: string[] }
  unassignedTaskIds: string[]
  projection: KeyResultProjection
}

function sortedIds(ids: readonly string[]): string[] {
  return [...ids].sort((left, right) => (left < right ? -1 : left > right ? 1 : 0))
}

function singletonObjectives(objectives: readonly Objective[]): Objective[] {
  const grouped = new Map<string, Objective[]>()
  for (const objective of objectives) {
    const bucket = grouped.get(objective.id)
    if (bucket) bucket.push(objective)
    else grouped.set(objective.id, [objective])
  }
  return [...grouped.entries()]
    .filter(([, records]) => records.length === 1)
    .map(([, records]) => records[0])
    .sort((left, right) => (left.id < right.id ? -1 : left.id > right.id ? 1 : 0))
}

function hasNoKeyResultRefs(task: Task): boolean {
  return (task.key_result_refs ?? []).length === 0
}

export function projectOutcomeHierarchy(input: KeyResultProjectionInput): OutcomeHierarchy {
  const projection = projectKeyResults(input)
  const krByObjective = new Map<string, OutcomeHierarchyKeyResultNode[]>()
  for (const node of projection.keyResults) {
    const linkedTaskIds = sortedIds(node.linkedTaskIds)
    const visibleTaskIds = sortedIds(node.visibleTaskIds)
    const copied: OutcomeHierarchyKeyResultNode = {
      key: node.key,
      objectiveId: node.objectiveId,
      keyResultId: node.keyResultId,
      recordedProgress: node.recordedProgress,
      linkedTaskIds,
      visibleTaskIds,
      counts: { ...node.counts },
      visibleCounts: { ...node.visibleCounts },
      taskRefs: linkedTaskIds.map((taskId) => ({ taskId })),
    }
    const bucket = krByObjective.get(node.objectiveId)
    if (bucket) bucket.push(copied)
    else krByObjective.set(node.objectiveId, [copied])
  }
  for (const bucket of krByObjective.values()) {
    bucket.sort((left, right) =>
      left.keyResultId < right.keyResultId ? -1 : left.keyResultId > right.keyResultId ? 1 : 0,
    )
  }

  const objectiveNodes: OutcomeHierarchyObjectiveNode[] = singletonObjectives(input.objectives).map(
    (objective) => {
      const objectiveOnlyTaskIds = sortedIds(
        input.tasks
          .filter(
            (task) => hasNoKeyResultRefs(task) && task.objective_ids.includes(objective.id),
          )
          .map((task) => task.id),
      )
      return {
        objectiveId: objective.id,
        keyResults: krByObjective.get(objective.id) ?? [],
        objectiveOnlyTaskIds,
      }
    },
  )

  const unalignedTaskIds = sortedIds(
    input.tasks
      .filter((task) => task.objective_ids.length === 0 && hasNoKeyResultRefs(task))
      .map((task) => task.id),
  )

  const unresolvedTaskIds = sortedIds(
    projection.tasks.filter((entry) => entry.unresolvedRefs.length > 0).map((entry) => entry.taskId),
  )

  return {
    workspaceId: projection.workspaceId,
    objectiveNodes,
    unaligned: { taskIds: unalignedTaskIds },
    unresolved: { taskIds: unresolvedTaskIds },
    unassignedTaskIds: [...projection.unassignedTaskIds],
    projection,
  }
}
