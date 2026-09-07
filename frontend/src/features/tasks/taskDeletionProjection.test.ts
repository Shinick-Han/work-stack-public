import type { Objective, Task, WorkspaceProjection } from '../../domain/types'
import { workspace } from '../../test/fixtures'
import { removeDeletedTaskFromWorkspace } from './taskDeletionProjection'

const sibling: Task = {
  ...workspace.tasks[0],
  id: 'T-0002',
  uid: '22222222-2222-2222-8222-222222222222',
  title: 'Keep sibling',
  revision: 4,
}

const target = workspace.tasks[0]

const objective: Objective = {
  id: 'O-1',
  objective: 'Release quality customers trust',
  status: 'active',
  revision: 3,
  key_results: [
    { id: 'KR-1', text: 'Gate coverage', target: '90', progress: 40 },
    { id: 'KR-2', text: 'Unrecorded', target: '1' },
  ],
}

const populated: WorkspaceProjection = {
  ...workspace,
  tasks: [target, sibling],
  objectives: [objective],
  notes: [{ id: 'N-1', text: 'Keep this note', links: [target.id] }],
  inbox_count: 4,
  edges: [
    { source: target.id, target: sibling.id, kind: 'blocks' },
    { source: sibling.id, target: 'T-0003', kind: 'parent' },
  ],
}

test('removes only the deleted Task and incident edges', () => {
  const result = removeDeletedTaskFromWorkspace(populated, target.id)

  expect(result.tasks.map((item) => item.id)).toEqual([sibling.id])
  expect(result.edges).toEqual([{ source: sibling.id, target: 'T-0003', kind: 'parent' }])
  expect(result.objectives).toEqual(populated.objectives)
  expect(result.objectives[0].key_results?.[0].progress).toBe(40)
  expect(result.notes).toEqual(populated.notes)
  expect(result.inbox_count).toBe(4)
  expect(result.workspace).toEqual(populated.workspace)
  expect(populated.tasks.map((item) => item.id)).toEqual([target.id, sibling.id])
  expect(populated.edges).toHaveLength(2)
})
