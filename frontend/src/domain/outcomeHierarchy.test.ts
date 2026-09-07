import { describe, expect, it } from 'vitest'

import { keyResultKey, projectKeyResults } from './keyResultModel'
import { UNASSIGNED_TASK_IDS_COMPAT, projectOutcomeHierarchy } from './outcomeHierarchy'
import type { Objective, Task } from './types'
import {
  DERIVED_KEY_RESULT_OBJECTIVE,
  DERIVED_OBJECTIVE_TASK,
  DERIVED_TASK_KEY_RESULT,
  deriveOutcomeEdges,
  keyResultEndpointKey,
  objectiveEndpointKey,
  taskEndpointKey,
} from '../features/workspace/views/keyResultViewModel'
import type { WorkspaceTask } from '../features/workspace/views/types'

/**
 * Wave 1 K1 — plan §1.3 / §6 K0–K1 / §8.4.
 * Pure domain hierarchy. Graph/write-boundary remain later-wave RED.
 */

const WORKSPACE_ID = 'W-oracle'

function objective(
  id: string,
  keyResults: { id: string; text: string; progress?: number }[],
): Objective {
  return {
    id,
    objective: `${id} objective`,
    title: `${id} objective`,
    revision: 1,
    key_results: keyResults.map((item) => ({
      id: item.id,
      text: item.text,
      ...(item.progress !== undefined ? { progress: item.progress } : {}),
    })),
  }
}

function task(id: string, patch: Partial<Task> = {}): Task {
  return {
    id,
    uid: `00000000-0000-4000-8000-${id.replace(/[^0-9]/g, '').padStart(12, '0')}`,
    title: `${id} title`,
    detail: '',
    status: 'open',
    priority: 'P2',
    due: null,
    tags: [],
    objective_ids: [],
    parent_id: null,
    dependencies: [],
    subtasks: [],
    notes: [],
    revision: 1,
    context_count: 0,
    ...patch,
  }
}

/** Plan §8.4 fixture. O-1/KR-1 recorded progress is 40 so status-only drift is observable. */
function section84Fixture() {
  const objectives: Objective[] = [
    objective('O-1', [
      { id: 'KR-1', text: 'O-1 KR-1', progress: 40 },
      { id: 'KR-2', text: 'O-1 KR-2' },
    ]),
    objective('O-2', [{ id: 'KR-1', text: 'O-2 KR-1' }]),
  ]
  const tasks: Task[] = [
    task('T-1', {
      objective_ids: ['O-1'],
      key_result_refs: [{ objective_id: 'O-1', key_result_id: 'KR-1' }],
    }),
    task('T-2', { objective_ids: ['O-1'] }),
    task('T-3', {
      objective_ids: ['O-1'],
      key_result_refs: [
        { objective_id: 'O-1', key_result_id: 'KR-1' },
        { objective_id: 'O-1', key_result_id: 'KR-2' },
      ],
    }),
    task('T-4'),
    task('T-5', {
      objective_ids: ['O-1'],
      key_result_refs: [{ objective_id: 'O-1', key_result_id: 'KR-MISSING' }],
    }),
  ]
  return { objectives, tasks }
}

function asWorkspace(tasks: readonly Task[]): readonly WorkspaceTask[] {
  return tasks as unknown as readonly WorkspaceTask[]
}

function hierarchyOf(visibleTasks?: readonly Task[]) {
  const { objectives, tasks } = section84Fixture()
  return projectOutcomeHierarchy({
    workspaceId: WORKSPACE_ID,
    tasks,
    visibleTasks: visibleTasks ?? tasks,
    objectives,
  })
}

describe('pure hierarchy', () => {
  it('§8.4 fixture: O-1 has KR-1/KR-2, O-2 has zero-linked KR-1, T-2 objective-only, T-4 floating, T-5 unresolved', () => {
    const tree = hierarchyOf()
    const o1 = tree.objectiveNodes.find((node) => node.objectiveId === 'O-1')
    const o2 = tree.objectiveNodes.find((node) => node.objectiveId === 'O-2')
    expect(tree.objectiveNodes.map((node) => node.objectiveId)).toEqual(['O-1', 'O-2'])
    expect(o1?.keyResults.map((node) => node.keyResultId)).toEqual(['KR-1', 'KR-2'])
    expect(o1?.objectiveOnlyTaskIds).toEqual(['T-2'])
    expect(o2?.keyResults).toHaveLength(1)
    expect(o2?.keyResults[0]?.linkedTaskIds).toEqual([])
    expect(o2?.keyResults[0]?.key).toBe(keyResultKey(WORKSPACE_ID, 'O-2', 'KR-1'))
    expect(tree.unaligned.taskIds).toEqual(['T-4'])
    expect(tree.unresolved.taskIds).toEqual(['T-5'])
    expect(tree.unassignedTaskIds).toEqual(['T-2', 'T-4'])
    expect(UNASSIGNED_TASK_IDS_COMPAT).toContain('Objective-only')
  })

  it('O-1/KR-1 and O-2/KR-1 identities differ', () => {
    const tree = hierarchyOf()
    const a = keyResultKey(WORKSPACE_ID, 'O-1', 'KR-1')
    const b = keyResultKey(WORKSPACE_ID, 'O-2', 'KR-1')
    expect(a).not.toBe(b)
    expect(tree.objectiveNodes[0]?.keyResults[0]?.key).toBe(a)
    expect(tree.objectiveNodes[1]?.keyResults[0]?.key).toBe(b)
  })

  it('hiding T-1 changes only visible counts, not total counts or recorded progress', () => {
    const { tasks } = section84Fixture()
    const hidden = hierarchyOf(tasks.filter((item) => item.id !== 'T-1'))
    const o1kr1 = hidden.objectiveNodes[0]?.keyResults.find((node) => node.keyResultId === 'KR-1')
    expect(o1kr1?.recordedProgress).toBe(40)
    expect(o1kr1?.counts.total).toBe(2)
    expect(o1kr1?.visibleCounts.total).toBe(1)
    expect(o1kr1?.visibleTaskIds).toEqual(['T-3'])
    expect(o1kr1?.linkedTaskIds).toEqual(['T-1', 'T-3'])
  })

  it('shuffled task/objective input produces the same canonical hierarchy ordering', () => {
    const { objectives, tasks } = section84Fixture()
    const forward = projectOutcomeHierarchy({ workspaceId: WORKSPACE_ID, tasks, objectives })
    const shuffled = projectOutcomeHierarchy({
      workspaceId: WORKSPACE_ID,
      tasks: [...tasks].reverse(),
      objectives: [...objectives].reverse(),
    })
    const canon = (tree: ReturnType<typeof projectOutcomeHierarchy>) =>
      JSON.stringify({
        objectiveNodes: tree.objectiveNodes,
        unaligned: tree.unaligned,
        unresolved: tree.unresolved,
      })
    expect(canon(shuffled)).toBe(canon(forward))
  })

  it('inputs remain deeply unchanged', () => {
    const { objectives, tasks } = section84Fixture()
    const before = JSON.stringify({ objectives, tasks })
    projectOutcomeHierarchy({ workspaceId: WORKSPACE_ID, tasks, objectives })
    expect(JSON.stringify({ objectives, tasks })).toBe(before)
  })

  it('KR recorded progress does not change when only Task status changes', () => {
    const { objectives, tasks } = section84Fixture()
    const before = projectOutcomeHierarchy({ workspaceId: WORKSPACE_ID, tasks, objectives })
    const afterTasks = tasks.map((item) =>
      item.id === 'T-1' ? { ...item, status: 'done' as const } : item,
    )
    const after = projectOutcomeHierarchy({
      workspaceId: WORKSPACE_ID,
      tasks: afterTasks,
      objectives,
    })
    const key = keyResultKey(WORKSPACE_ID, 'O-1', 'KR-1')
    const pick = (tree: ReturnType<typeof projectOutcomeHierarchy>) =>
      tree.objectiveNodes[0]?.keyResults.find((node) => node.key === key)
    expect(pick(before)?.recordedProgress).toBe(40)
    expect(pick(after)?.recordedProgress).toBe(40)
    expect(pick(after)?.counts.done).toBe(1)
  })

  it('multi-KR T-3 remains one canonical Task referenced under each KR', () => {
    const tree = hierarchyOf()
    const o1 = tree.objectiveNodes[0]
    expect(o1?.keyResults[0]?.taskRefs.map((ref) => ref.taskId)).toEqual(['T-1', 'T-3'])
    expect(o1?.keyResults[1]?.taskRefs.map((ref) => ref.taskId)).toEqual(['T-3'])
    const ids = tree.projection.tasks.map((entry) => entry.taskId)
    expect(ids.filter((id) => id === 'T-3')).toHaveLength(1)
  })

  it('read projection does not auto-align a KR ref missing its parent Objective (write path is WorkStack.patch_task)', () => {
    const { objectives, tasks } = section84Fixture()
    const orphan = task('T-link', {
      objective_ids: [],
      key_result_refs: [{ objective_id: 'O-1', key_result_id: 'KR-1' }],
    })
    const tree = projectOutcomeHierarchy({
      workspaceId: WORKSPACE_ID,
      tasks: [...tasks, orphan],
      objectives,
    })
    const entry = tree.projection.tasks.find((item) => item.taskId === 'T-link')
    expect(entry?.refs[0]?.resolved).toBe(false)
    expect(entry?.refs[0]).toMatchObject({ reason: 'unaligned-parent' })
    expect(tree.unresolved.taskIds).toContain('T-link')
    expect(tree.unaligned.taskIds).not.toContain('T-link')
  })

  it('legacy unassignedTaskIds still includes Objective-only T-2', () => {
    const projection = projectKeyResults({
      workspaceId: WORKSPACE_ID,
      ...section84Fixture(),
    })
    expect(projection.unassignedTaskIds).toEqual(['T-2', 'T-4'])
  })

  it('floating T-4 invents no Objective or KR', () => {
    const tree = hierarchyOf()
    expect(tree.objectiveNodes.map((node) => node.objectiveId)).toEqual(['O-1', 'O-2'])
    expect(
      tree.objectiveNodes.flatMap((node) => node.keyResults.map((kr) => `${kr.objectiveId}/${kr.keyResultId}`)),
    ).toEqual(['O-1/KR-1', 'O-1/KR-2', 'O-2/KR-1'])
    expect(tree.unaligned.taskIds).toEqual(['T-4'])
  })

  it('K2 write invariant is WorkStack.patch_task, not a domain alignment export', async () => {
    const domain = await import('./outcomeHierarchy')
    expect(Object.keys(domain)).not.toContain('applyKeyResultParentAlignment')
    expect(typeof domain.projectOutcomeHierarchy).toBe('function')
  })
})

describe('Graph derived-edge direction', () => {
  it('Graph derived edges are Objective→KR and KR→Task (O-1→KR-1→T-1 once)', () => {
    const { objectives, tasks } = section84Fixture()
    const projection = projectKeyResults({ workspaceId: WORKSPACE_ID, tasks, objectives })
    const edges = deriveOutcomeEdges(projection, asWorkspace(tasks))
    const o1kr1 = keyResultKey(WORKSPACE_ID, 'O-1', 'KR-1')

    const objectiveToKr = edges.filter(
      (edge) =>
        edge.source === objectiveEndpointKey('O-1') && edge.target === keyResultEndpointKey(o1kr1),
    )
    const krToTask = edges.filter(
      (edge) =>
        edge.source === keyResultEndpointKey(o1kr1) && edge.target === taskEndpointKey('T-1'),
    )

    expect(objectiveToKr).toHaveLength(1)
    expect(krToTask).toHaveLength(1)
  })

  it('a zero-linked KR keeps its Objective→KR edge and fabricates no Task', () => {
    const { objectives, tasks } = section84Fixture()
    const projection = projectKeyResults({ workspaceId: WORKSPACE_ID, tasks, objectives })
    const edges = deriveOutcomeEdges(projection, asWorkspace(tasks))
    const o2kr1 = keyResultKey(WORKSPACE_ID, 'O-2', 'KR-1')
    expect(
      edges.filter(
        (edge) =>
          edge.kind === DERIVED_KEY_RESULT_OBJECTIVE
          && edge.source === objectiveEndpointKey('O-2')
          && edge.target === keyResultEndpointKey(o2kr1),
      ),
    ).toHaveLength(1)
    expect(edges.filter((edge) => edge.target === taskEndpointKey('T-4'))).toHaveLength(0)
    expect(edges.some((edge) => edge.source.includes('KR-MISSING') || edge.target.includes('KR-MISSING'))).toBe(false)
  })

  it('Objective-only T-2 is Objective→Task; multi-KR T-3 shares one Task endpoint', () => {
    const { objectives, tasks } = section84Fixture()
    const projection = projectKeyResults({ workspaceId: WORKSPACE_ID, tasks, objectives })
    const edges = deriveOutcomeEdges(projection, asWorkspace(tasks))
    expect(
      edges.filter(
        (edge) =>
          edge.kind === DERIVED_OBJECTIVE_TASK
          && edge.source === objectiveEndpointKey('O-1')
          && edge.target === taskEndpointKey('T-2'),
      ),
    ).toHaveLength(1)
    expect(edges.filter((edge) => edge.kind === DERIVED_OBJECTIVE_TASK && edge.target === taskEndpointKey('T-1'))).toHaveLength(0)
    const t3 = edges.filter(
      (edge) => edge.kind === DERIVED_TASK_KEY_RESULT && edge.target === taskEndpointKey('T-3'),
    )
    expect(t3).toHaveLength(2)
  })

  it('does not mutate the visible Task array', () => {
    const { objectives, tasks } = section84Fixture()
    const projection = projectKeyResults({ workspaceId: WORKSPACE_ID, tasks, objectives })
    const visible = asWorkspace(tasks)
    const before = JSON.stringify(visible)
    deriveOutcomeEdges(projection, visible)
    expect(JSON.stringify(visible)).toBe(before)
  })
})
