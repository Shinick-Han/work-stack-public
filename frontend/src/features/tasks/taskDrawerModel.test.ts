import { expect, test } from 'vitest'
import type { ContextItem, Task } from '../../domain/types'
import { task } from '../../test/fixtures'
import {
  activityTitle,
  contextTitle,
  createSaveRun,
  externalContext,
  overlayDirtyFields,
  pruneServerEqualFields,
  sameValue,
} from './taskDrawerModel'

test('creates isolated save runs and removes only fields already confirmed by the server', () => {
  const first = createSaveRun(task.id)
  const second = createSaveRun(task.id)
  first.dirtyFields.add('title')

  expect(second.dirtyFields.size).toBe(0)
  expect(pruneServerEqualFields({
    title: task.title,
    priority: task.priority === 'P0' ? 'P1' : 'P0',
  }, task)).toEqual({
    priority: task.priority === 'P0' ? 'P1' : 'P0',
  })
})

test('overlays only explicitly dirty draft fields onto a newer authoritative Task', () => {
  const newer = { ...task, title: 'Authoritative title', priority: 'P1' } satisfies Task
  const draft = { ...task, title: 'Local title', priority: 'P3' } satisfies Task

  expect(overlayDirtyFields(newer, draft, new Set(['title']))).toMatchObject({
    title: 'Local title',
    priority: 'P1',
  })
})

test('keeps external context and planning-status labels deterministic', () => {
  const context = {
    id: 'C-1',
    kind: 'capture',
    source: { display_title: 'Mail subject' },
  } as ContextItem

  expect(externalContext(context)).toBe(true)
  expect(contextTitle(context)).toBe('Mail subject')
  expect(activityTitle({
    type: 'task.planning_status',
    status: 'done',
    prior_status: 'started',
  } as never)).toBe('In progress → Done')
})


test('sameValue compares scoped ref pairs structurally while keeping primitive behaviour', () => {
  const confirmed: Task = {
    ...task,
    key_result_refs: [
      { objective_id: 'O-0001', key_result_id: 'KR-1' },
      { objective_id: 'O-0002', key_result_id: 'KR-1' },
    ],
  }
  const reloaded = JSON.parse(JSON.stringify(confirmed.key_result_refs)) as Task['key_result_refs']

  expect(sameValue(reloaded, confirmed.key_result_refs)).toBe(true)
  expect(sameValue([{ objective_id: 'O-0001', key_result_id: 'KR-2' }], confirmed.key_result_refs)).toBe(false)
  expect(sameValue(['A', 'B'], ['A', 'B'])).toBe(true)
  expect(sameValue(['A'], ['B'])).toBe(false)
  expect(sameValue(2, 2)).toBe(true)
  expect(sameValue({ objective_id: 'O-0001' }, { objective_id: 'O-0001' })).toBe(false)
  expect(sameValue(
    [{ objective_id: 'O-0001', key_result_id: 'KR-1', extra: 'x' }],
    [{ objective_id: 'O-0001', key_result_id: 'KR-1' }],
  )).toBe(false)
})

test('an already confirmed ref set prunes away, while omission and explicit clear stay distinct', () => {
  const confirmed: Task = {
    ...task,
    key_result_refs: [{ objective_id: 'O-0001', key_result_id: 'KR-1' }],
  }
  const reloaded = JSON.parse(JSON.stringify(confirmed)) as Task

  expect(pruneServerEqualFields({ key_result_refs: reloaded.key_result_refs }, confirmed)).toEqual({})
  expect(pruneServerEqualFields({ key_result_refs: [] }, confirmed)).toEqual({ key_result_refs: [] })
  expect(pruneServerEqualFields({ key_result_refs: [] }, task)).toEqual({ key_result_refs: [] })
})
