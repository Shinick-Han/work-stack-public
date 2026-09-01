import { expect, test } from 'vitest'
import { capture, task, workspace } from '../../test/fixtures'
import { selectTaskDrawerData } from './taskDrawerSelectors'

test('derives relationship choices and provider-gated reply sources in one pass', () => {
  const parent = { ...task, id: 'T-0002', title: 'Parent' }
  const dependency = { ...task, id: 'T-0003', title: 'Dependency' }
  const child = { ...task, id: 'T-0004', title: 'Child', parent_id: task.id }
  const dependent = { ...task, id: 'T-0005', title: 'Dependent', dependencies: [task.id] }
  const draft = { ...task, parent_id: parent.id, dependencies: [dependency.id] }
  const outlook = { ...capture, id: 'C-0001' }
  const teams = {
    ...capture,
    id: 'C-0002',
    source: { ...capture.source, provider: 'microsoft-teams' as const, display_title: 'Teams thread' },
  }

  const selected = selectTaskDrawerData({
    context: [outlook, teams],
    draft,
    providerGates: {
      'microsoft-outlook': { read: true, reply: true },
      'microsoft-teams': { read: true, reply: false },
    },
    workspace: { ...workspace, tasks: [draft, parent, dependency, child, dependent] },
  })

  expect(selected.taskObjectives.map((objective) => objective.id)).toEqual(['O-1'])
  expect(selected.parentTask?.id).toBe(parent.id)
  expect(selected.dependencyTasks.map((item) => item.id)).toEqual([dependency.id])
  expect(selected.childTasks.map((item) => item.id)).toEqual([child.id])
  expect(selected.dependentTasks.map((item) => item.id)).toEqual([dependent.id])
  expect(selected.availableParentTasks.map((item) => item.id)).not.toContain(task.id)
  expect(selected.availableDependencyTasks.map((item) => item.id)).not.toEqual(expect.arrayContaining([task.id, dependency.id]))
  expect(selected.replySources.map((source) => source.capture_id)).toEqual(['C-0001'])
  expect(selected.replyUnavailableSources.map((source) => source.capture_id)).toEqual(['C-0002'])
})

test('returns stable empty selections before a Task draft exists', () => {
  const selected = selectTaskDrawerData({
    context: [],
    draft: null,
    providerGates: {
      'microsoft-outlook': { read: false, reply: false },
      'microsoft-teams': { read: false, reply: false },
    },
    workspace,
  })

  expect(selected.parentTask).toBeNull()
  expect(selected.taskObjectives).toEqual([])
  expect(selected.availableParentTasks).toEqual([])
  expect(selected.replySources).toEqual([])
})
