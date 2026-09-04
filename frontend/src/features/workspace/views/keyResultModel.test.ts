import { describe, expect, test } from 'vitest'
import type { Objective, Task } from '../../../domain/types'
import {
  eligibleKeyResults,
  keyResultKey,
  matchesOutcomeFilter,
  projectKeyResults,
} from './keyResultModel'

function objective(id: string, keyResults: Objective['key_results'], overrides: Partial<Objective> = {}): Objective {
  return { id, objective: `${id} objective`, revision: 3, key_results: keyResults, ...overrides }
}

function task(id: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    uid: `00000000-0000-4000-8000-${id.replace(/\D/g, '').padStart(12, '0')}`,
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
    ...overrides,
  }
}

const FIRST = objective('O-0001', [
  { id: 'KR-1', text: 'First outcome', target: '10', progress: 0 },
  { id: 'KR-2', text: 'Second outcome' },
])
const SECOND = objective('O-0002', [{ id: 'KR-1', text: 'Other outcome', progress: 40 }])

describe('scoped identity', () => {
  test('the same local KR ID under two Objectives yields distinct keys', () => {
    const projection = projectKeyResults({ workspaceId: 'W1', tasks: [], objectives: [FIRST, SECOND] })

    const keys = projection.keyResults.map((node) => node.key)
    expect(new Set(keys).size).toBe(keys.length)
    expect(keys).toContain(keyResultKey('W1', 'O-0001', 'KR-1'))
    expect(keys).toContain(keyResultKey('W1', 'O-0002', 'KR-1'))
  })

  test('workspace context changes the key while text and progress never enter identity', () => {
    const here = projectKeyResults({ workspaceId: 'W1', tasks: [], objectives: [FIRST] })
    const there = projectKeyResults({ workspaceId: 'W2', tasks: [], objectives: [FIRST] })
    const renamed = projectKeyResults({
      workspaceId: 'W1',
      tasks: [],
      objectives: [objective('O-0001', [{ id: 'KR-1', text: 'Renamed', progress: 99, status: 'done' }])],
    })

    expect(here.keyResults[0].key).not.toBe(there.keyResults[0].key)
    expect(renamed.keyResults[0].key).toBe(here.keyResults[0].key)
  })
})

describe('nodes and counts', () => {
  test('zero-linked KRs are returned and unrecorded progress stays null, not zero', () => {
    const projection = projectKeyResults({ workspaceId: 'W1', tasks: [], objectives: [FIRST] })

    const [recorded, unrecorded] = projection.keyResults
    expect(recorded.recordedProgress).toBe(0)
    expect(unrecorded.recordedProgress).toBeNull()
    expect(unrecorded.counts).toEqual({ total: 0, open: 0, started: 0, done: 0, dropped: 0 })
    expect(unrecorded.target).toBeNull()
    expect(recorded.objectiveRevision).toBe(3)
  })

  test('every status is counted and the visible subset is counted separately', () => {
    const linked = (id: string, status: Task['status']) => task(id, {
      status,
      objective_ids: ['O-0001'],
      key_result_refs: [{ objective_id: 'O-0001', key_result_id: 'KR-1' }],
    })
    const tasks = [linked('T-1', 'open'), linked('T-2', 'started'), linked('T-3', 'done'), linked('T-4', 'dropped')]

    const projection = projectKeyResults({ workspaceId: 'W1', tasks, visibleTasks: [], objectives: [FIRST] })

    const node = projection.byKey[keyResultKey('W1', 'O-0001', 'KR-1')]
    expect(node.counts).toEqual({ total: 4, open: 1, started: 1, done: 1, dropped: 1 })
    expect(node.visibleCounts.total).toBe(0)
    expect(node.linkedTaskIds).toEqual(['T-1', 'T-2', 'T-3', 'T-4'])
    expect(node.visibleTaskIds).toEqual([])
    expect(node).not.toHaveProperty('completionPercent')
  })

  test('a Task with several outcomes counts once per KR but takes one placement', () => {
    const multi = task('T-9', {
      objective_ids: ['O-0001', 'O-0002'],
      key_result_refs: [
        { objective_id: 'O-0001', key_result_id: 'KR-1' },
        { objective_id: 'O-0002', key_result_id: 'KR-1' },
      ],
    })

    const projection = projectKeyResults({ workspaceId: 'W1', tasks: [multi], objectives: [FIRST, SECOND] })

    expect(projection.byKey[keyResultKey('W1', 'O-0001', 'KR-1')].counts.total).toBe(1)
    expect(projection.byKey[keyResultKey('W1', 'O-0002', 'KR-1')].counts.total).toBe(1)
    expect(projection.tasks).toHaveLength(1)
    expect(projection.tasks[0].placement).toBe('multiple-outcomes')
    expect(projection.tasks[0].resolvedKeys).toHaveLength(2)
  })
})

describe('unresolved classification', () => {
  const cases: Array<[string, Objective[], string]> = [
    ['missing-objective', [SECOND], 'missing-objective'],
    ['ambiguous-objective', [FIRST, objective('O-0001', [{ id: 'KR-1', text: 'Duplicate parent' }])], 'ambiguous-objective'],
    ['missing-key-result', [objective('O-0001', [{ id: 'KR-9', text: 'Elsewhere' }])], 'missing-key-result'],
    ['ambiguous-key-result', [objective('O-0001', [{ id: 'KR-1', text: 'A' }, { id: 'KR-1', text: 'B' }])], 'ambiguous-key-result'],
  ]

  test.each(cases)('%s keeps the original pair and chooses no record', (_name, objectives, reason) => {
    const linked = task('T-1', {
      objective_ids: ['O-0001'],
      key_result_refs: [{ objective_id: 'O-0001', key_result_id: 'KR-1' }],
    })

    const projection = projectKeyResults({ workspaceId: 'W1', tasks: [linked], objectives })

    const [ref] = projection.tasks[0].refs
    expect(ref.resolved).toBe(false)
    expect(ref).toMatchObject({ reason, ref: { objective_id: 'O-0001', key_result_id: 'KR-1' } })
    expect(projection.tasks[0].unresolvedRefs).toEqual([{ objective_id: 'O-0001', key_result_id: 'KR-1' }])
    expect(projection.tasks[0].placement).toBe('unresolved-outcome')
  })

  test('an unrelated duplicate parent does not hide unrelated valid outcomes', () => {
    const objectives = [FIRST, SECOND, objective('O-0002', [{ id: 'KR-1', text: 'Duplicate' }])]
    const linked = task('T-1', {
      objective_ids: ['O-0001'],
      key_result_refs: [{ objective_id: 'O-0001', key_result_id: 'KR-1' }],
    })

    const projection = projectKeyResults({ workspaceId: 'W1', tasks: [linked], objectives })

    expect(projection.tasks[0].placement).toBe('single-outcome')
    expect(projection.byKey[keyResultKey('W1', 'O-0001', 'KR-1')].counts.total).toBe(1)
    expect(projection.byKey[keyResultKey('W1', 'O-0002', 'KR-1')]).toBeUndefined()
  })
})

describe('unassigned and filtering', () => {
  test('legacy omitted and explicit empty are both unassigned, and an Objective-only Task stays visible', () => {
    const legacy = task('T-1', { objective_ids: ['O-0001'] })
    const cleared = task('T-2', { objective_ids: ['O-0001'], key_result_refs: [] })
    const operational = task('T-3')

    const projection = projectKeyResults({ workspaceId: 'W1', tasks: [legacy, cleared, operational], objectives: [FIRST] })

    expect(projection.unassignedTaskIds).toEqual(['T-1', 'T-2', 'T-3'])
    expect(projection.tasks.every((item) => item.placement === 'unassigned-outcome')).toBe(true)
    expect(legacy.key_result_refs).toBeUndefined()
    expect(cleared.key_result_refs).toEqual([])
  })

  test('the filter matches the exact pair, and a dangling pair is stored, not unassigned', () => {
    const linked = task('T-1', { key_result_refs: [{ objective_id: 'O-0001', key_result_id: 'KR-1' }] })
    const dangling = task('T-2', { key_result_refs: [{ objective_id: 'O-0404', key_result_id: 'KR-1' }] })

    expect(matchesOutcomeFilter(linked, { kind: 'all' })).toBe(true)
    expect(matchesOutcomeFilter(linked, { kind: 'pair', objectiveId: 'O-0001', keyResultId: 'KR-1' })).toBe(true)
    expect(matchesOutcomeFilter(linked, { kind: 'pair', objectiveId: 'O-0002', keyResultId: 'KR-1' })).toBe(false)
    expect(matchesOutcomeFilter(linked, { kind: 'unassigned' })).toBe(false)
    expect(matchesOutcomeFilter(dangling, { kind: 'unassigned' })).toBe(false)
    expect(matchesOutcomeFilter(dangling, { kind: 'pair', objectiveId: 'O-0404', keyResultId: 'KR-1' })).toBe(true)
  })
})

describe('purity', () => {
  test('frozen inputs are never mutated', () => {
    const linked = Object.freeze(task('T-1', {
      objective_ids: Object.freeze(['O-0001']) as unknown as string[],
      key_result_refs: Object.freeze([Object.freeze({ objective_id: 'O-0001', key_result_id: 'KR-1' })]) as unknown as Task['key_result_refs'],
    }))
    const objectives = Object.freeze([Object.freeze(FIRST)]) as unknown as Objective[]

    const projection = projectKeyResults({ workspaceId: 'W1', tasks: [linked], objectives })

    expect(projection.tasks[0].placement).toBe('single-outcome')
    expect(linked.key_result_refs).toEqual([{ objective_id: 'O-0001', key_result_id: 'KR-1' }])
    expect(FIRST.key_results).toHaveLength(2)
  })

  test('eligible outcomes are limited to aligned Objectives and unique KRs', () => {
    const eligible = eligibleKeyResults(['O-0001'], [FIRST, SECOND])

    expect(eligible.map((node) => `${node.objectiveId}/${node.keyResultId}`)).toEqual(['O-0001/KR-1', 'O-0001/KR-2'])
  })
})


describe('OE-02 parent alignment participates in resolution', () => {
  const linked = (objectiveIds: string[]) => task('T-1', {
    objective_ids: objectiveIds,
    key_result_refs: [{ objective_id: 'O-0001', key_result_id: 'KR-1' }],
  })

  test('a ref whose parent is not in the Task objective_ids stays unresolved with its pair', () => {
    const projection = projectKeyResults({
      workspaceId: 'W1',
      tasks: [linked(['O-0002'])],
      objectives: [FIRST, SECOND],
    })

    const [ref] = projection.tasks[0].refs
    expect(ref.resolved).toBe(false)
    expect(ref.ref).toEqual({ objective_id: 'O-0001', key_result_id: 'KR-1' })
    expect(projection.tasks[0].placement).toBe('unresolved-outcome')
    expect(projection.tasks[0].resolvedKeys).toEqual([])
  })

  test('a wrong-parent ref contributes no membership, count or area', () => {
    const projection = projectKeyResults({
      workspaceId: 'W1',
      tasks: [linked([])],
      objectives: [FIRST],
    })

    const node = projection.byKey[keyResultKey('W1', 'O-0001', 'KR-1')]
    expect(node.linkedTaskIds).toEqual([])
    expect(node.counts.total).toBe(0)
    expect(node.visibleTaskIds).toEqual([])
  })

  test('healthy control: the same pair resolves when the parent is aligned', () => {
    const projection = projectKeyResults({
      workspaceId: 'W1',
      tasks: [linked(['O-0001'])],
      objectives: [FIRST],
    })

    expect(projection.tasks[0].placement).toBe('single-outcome')
    expect(projection.byKey[keyResultKey('W1', 'O-0001', 'KR-1')].counts.total).toBe(1)
  })

  test('an unresolved wrong-parent pair is still matched as a stored reference', () => {
    const stored = linked(['O-0002'])

    expect(matchesOutcomeFilter(stored, { kind: 'pair', objectiveId: 'O-0001', keyResultId: 'KR-1' })).toBe(true)
    expect(matchesOutcomeFilter(stored, { kind: 'unassigned' })).toBe(false)
  })
})
