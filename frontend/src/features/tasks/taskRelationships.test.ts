import { describe, expect, test } from 'vitest'
import type { Task } from '../../domain/types'
import { blockingDependencies, cyclicRelationshipCandidates, relationshipWouldCycle } from '../../domain/taskRelationships'

function task(id: string, parentId: string | null = null, dependencies: string[] = []): Task {
  return {
    id,
    uid: `00000000-0000-4000-8000-${id.slice(2).padStart(12, '0')}`,
    title: id,
    detail: '',
    status: 'open',
    priority: 'P2',
    due: null,
    tags: [],
    objective_ids: [],
    parent_id: parentId,
    dependencies,
    subtasks: [],
    notes: [],
    revision: 0,
    context_count: 0,
  }
}

describe('relationshipWouldCycle', () => {
  const tasks = [
    task('T-0001'),
    task('T-0002', 'T-0001', ['T-0001']),
    task('T-0003', 'T-0002', ['T-0002']),
    task('T-0004'),
  ]

  test('finds transitive parent and dependency cycles', () => {
    expect(relationshipWouldCycle(tasks, 'T-0001', 'T-0003', 'parent_id')).toBe(true)
    expect(relationshipWouldCycle(tasks, 'T-0001', 'T-0003', 'dependencies')).toBe(true)
  })

  test('returns the complete candidate set in one graph traversal', () => {
    expect([...cyclicRelationshipCandidates(tasks, 'T-0001', 'parent_id')].sort()).toEqual(['T-0002', 'T-0003'])
    expect([...cyclicRelationshipCandidates(tasks, 'T-0001', 'dependencies')].sort()).toEqual(['T-0002', 'T-0003'])
  })

  test('allows unrelated candidates and bounds pre-existing cycles', () => {
    expect(relationshipWouldCycle(tasks, 'T-0001', 'T-0004', 'parent_id')).toBe(false)
    expect(relationshipWouldCycle(tasks, 'T-0001', 'T-0004', 'dependencies')).toBe(false)
    const cyclic = [task('T-0001'), task('T-0002', 'T-0003'), task('T-0003', 'T-0002')]
    expect(relationshipWouldCycle(cyclic, 'T-0001', 'T-0002', 'parent_id')).toBe(false)
  })

  test('keeps a ten-thousand Task chain within the bounded picker gate', () => {
    const large = Array.from({ length: 10_000 }, (_, index) => {
      const id = `T-${String(index + 1).padStart(5, '0')}`
      const parent = index ? `T-${String(index).padStart(5, '0')}` : null
      return task(id, parent)
    })
    const started = performance.now()
    const candidates = cyclicRelationshipCandidates(large, 'T-00001', 'parent_id')
    const elapsed = performance.now() - started

    expect(candidates.size).toBe(9_999)
    expect(elapsed).toBeLessThan(500)
  })
})

describe('blockingDependencies', () => {
  test('treats only done dependencies as satisfied and preserves missing evidence', () => {
    const subject = task('T-0001', null, ['T-0002', 'T-0003', 'T-0004', 'T-9999'])
    const blockers = blockingDependencies([
      subject,
      task('T-0002'),
      { ...task('T-0003'), status: 'done' },
      { ...task('T-0004'), status: 'dropped' },
    ], subject)

    expect(blockers).toEqual([
      { id: 'T-0002', title: 'T-0002', status: 'open' },
      { id: 'T-0004', title: 'T-0004', status: 'dropped' },
      { id: 'T-9999', title: null, status: null },
    ])
  })
})
