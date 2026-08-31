import { describe, expect, test } from 'vitest'
import type { Task } from '../../domain/types'
import {
  FocusModelError,
  addCalendarDays,
  buildFocusProjection,
  civilDayDistance,
  localIsoDate,
  parseCanonicalCivilDate,
} from './focusModel'

function makeTask(id: string, changes: Partial<Task> = {}): Task {
  return {
    id,
    uid: '11111111-1111-1111-8111-111111111111',
    title: `Task ${id}`,
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
    revision: 0,
    context_count: 0,
    ...changes,
  }
}

describe('canonical civil dates', () => {
  test('accepts real canonical dates, including years affected by Date.UTC remapping', () => {
    expect(parseCanonicalCivilDate('0001-01-01')?.iso).toBe('0001-01-01')
    expect(parseCanonicalCivilDate('0004-02-29')?.day).toBe(29)
    expect(parseCanonicalCivilDate('0099-12-31')?.year).toBe(99)
    expect(parseCanonicalCivilDate('0100-01-01')?.year).toBe(100)
    expect(parseCanonicalCivilDate('2024-02-29')?.iso).toBe('2024-02-29')
  })

  test.each([
    '20260829',
    '2026-W35-6',
    '2026-8-29',
    '2026-02-30',
    '2025-02-29',
    '0000-01-01',
  ])('rejects non-canonical or impossible date %s', (value) => {
    expect(parseCanonicalCivilDate(value)).toBeNull()
  })

  test('uses calendar arithmetic across leap, month, year, and DST-shaped boundaries', () => {
    expect(addCalendarDays('2024-02-28', 1)).toBe('2024-02-29')
    expect(addCalendarDays('2024-02-28', 2)).toBe('2024-03-01')
    expect(addCalendarDays('2026-12-31', 1)).toBe('2027-01-01')
    expect(civilDayDistance('2026-03-07', '2026-03-09')).toBe(2)
    expect(civilDayDistance('2026-10-31', '2026-11-02')).toBe(2)
  })

  test('builds today from local components instead of a UTC date string', () => {
    expect(localIsoDate(new Date(2026, 7, 29, 0, 5))).toBe('2026-08-29')
  })
})

describe('buildFocusProjection', () => {
  test('keeps every independent reason on one active candidate and excludes terminal tasks', () => {
    const tasks = [
      makeTask('T-0001', {
        due: '2026-08-27',
        priority: 'P0',
        status: 'started',
      }),
      makeTask('T-0002'),
      makeTask('T-0003', {
        due: '2026-08-29',
        priority: 'P0',
        status: 'done',
      }),
    ]

    const projection = buildFocusProjection(tasks, '2026-08-29')

    expect(projection.activeCount).toBe(2)
    expect(projection.focusCount).toBe(1)
    expect(projection.candidates).toHaveLength(1)
    expect(projection.candidates[0].task.id).toBe('T-0001')
    expect(projection.candidates[0].reasons.map((item) => item.kind)).toEqual([
      'overdue',
      'high_priority',
      'in_progress',
    ])
    expect(projection.candidates[0].reasons[0]).toMatchObject({
      label: '2 days overdue',
      days: 2,
    })
  })

  test.each([
    '20260829',
    '2026-W35-6',
    '2026-02-30',
    '2025-02-29',
  ])('surfaces invalid due %s without hiding status or priority reasons', (due) => {
    const projection = buildFocusProjection([
      makeTask('T-0001', { due, priority: 'P1', status: 'started' }),
    ], '2026-08-29')

    expect(projection.candidates[0].reasons.map((item) => item.kind)).toEqual([
      'invalid_due',
      'high_priority',
      'in_progress',
    ])
    expect(projection.candidates[0].reasons[0].label).toBe('Due date needs review')
  })

  test('applies overdue, today, +1, +7, and +8 boundaries exactly', () => {
    const projection = buildFocusProjection([
      makeTask('T-overdue', { due: '2026-08-28' }),
      makeTask('T-today', { due: '2026-08-29' }),
      makeTask('T-plus-1', { due: '2026-08-30' }),
      makeTask('T-plus-7', { due: '2026-09-05' }),
      makeTask('T-plus-8-hidden', { due: '2026-09-06' }),
      makeTask('T-plus-8-high', { due: '2026-09-06', priority: 'P1' }),
    ], '2026-08-29')

    const reasons = Object.fromEntries(projection.candidates.map((candidate) => [
      candidate.task.id,
      candidate.reasons.map((item) => item.kind),
    ]))
    expect(reasons).toMatchObject({
      'T-overdue': ['overdue'],
      'T-today': ['due_today'],
      'T-plus-1': ['due_soon'],
      'T-plus-7': ['due_soon'],
      'T-plus-8-high': ['high_priority'],
    })
    expect(reasons).not.toHaveProperty('T-plus-8-hidden')
  })

  test('sorts by urgency, due, priority, status, and ASCII task ID', () => {
    const projection = buildFocusProjection([
      makeTask('T-0010', { due: '2026-08-30', priority: 'P1' }),
      makeTask('T-0002', { due: '2026-08-30', priority: 'P1' }),
      makeTask('T-status-open', { priority: 'P1', status: 'open' }),
      makeTask('T-status-started', { priority: 'P1', status: 'started' }),
      makeTask('T-priority-p1', { due: '2026-08-31', priority: 'P1' }),
      makeTask('T-priority-p0', { due: '2026-08-31', priority: 'P0' }),
      makeTask('T-earlier', { due: '2026-08-30', priority: 'P3' }),
      makeTask('T-invalid', { due: '2026-02-30', priority: 'P3' }),
      makeTask('T-overdue', { due: '2026-08-28', priority: 'P3' }),
    ], '2026-08-29')

    expect(projection.candidates.map((candidate) => candidate.task.id)).toEqual([
      'T-invalid',
      'T-overdue',
      'T-0002',
      'T-0010',
      'T-earlier',
      'T-priority-p0',
      'T-priority-p1',
      'T-status-started',
      'T-status-open',
    ])
  })

  test('separates actionable candidates from tasks with unfinished dependencies', () => {
    const projection = buildFocusProjection([
      makeTask('T-blocked', { priority: 'P0', dependencies: ['T-prerequisite'] }),
      makeTask('T-actionable', { priority: 'P1' }),
      makeTask('T-prerequisite', { title: 'Finish prerequisite', status: 'open' }),
      makeTask('T-complete', { status: 'done' }),
      makeTask('T-ready', { priority: 'P2', due: '2026-08-29', dependencies: ['T-complete'] }),
    ], '2026-08-29')

    expect(projection.actionableCount).toBe(2)
    expect(projection.blockedCount).toBe(1)
    expect(projection.candidates.map((candidate) => candidate.task.id)).toEqual([
      'T-ready',
      'T-actionable',
      'T-blocked',
    ])
    expect(projection.candidates[2].blockers).toEqual([{
      id: 'T-prerequisite',
      title: 'Finish prerequisite',
      status: 'open',
    }])
  })

  test('fails safe when a dependency is missing or dropped', () => {
    const projection = buildFocusProjection([
      makeTask('T-missing', { priority: 'P1', dependencies: ['T-unknown'] }),
      makeTask('T-dropped', { status: 'dropped' }),
      makeTask('T-needs-dropped', { priority: 'P1', dependencies: ['T-dropped'] }),
    ], '2026-08-29')

    expect(projection.blockedCount).toBe(2)
    const blockers = Object.fromEntries(projection.candidates.map((candidate) => [candidate.task.id, candidate.blockers]))
    expect(blockers['T-needs-dropped']).toEqual([{
      id: 'T-dropped',
      title: 'Task T-dropped',
      status: 'dropped',
    }])
    expect(blockers['T-missing']).toEqual([{
      id: 'T-unknown',
      title: null,
      status: null,
    }])
  })

  test('throws a documented error for duplicate IDs instead of silently deduplicating', () => {
    expect(() => buildFocusProjection([
      makeTask('T-0001'),
      makeTask('T-0001', { status: 'done' }),
    ], '2026-08-29')).toThrow(FocusModelError)

    try {
      buildFocusProjection([makeTask('T-0001'), makeTask('T-0001')], '2026-08-29')
    } catch (error) {
      expect(error).toMatchObject({ code: 'duplicate_task_id' })
    }
  })

  test('does not mutate the input array or task objects', () => {
    const tasks = [
      makeTask('T-0002', { due: '2026-08-30', tags: ['second'] }),
      makeTask('T-0001', { due: '2026-08-28', tags: ['first'] }),
    ]
    const before = JSON.stringify(tasks)

    buildFocusProjection(tasks, '2026-08-29')

    expect(JSON.stringify(tasks)).toBe(before)
  })

  test('rejects an invalid injected today value', () => {
    expect(() => buildFocusProjection([makeTask('T-0001')], '2026-02-30')).toThrow(
      expect.objectContaining({ code: 'invalid_today' }),
    )
  })
})
