import { beforeEach, expect, test, vi } from 'vitest'
import type { AppUrlState } from '../../domain/types'
import { SAVED_FILTERS_KEY, filterCoordinates, readSavedFilters, sameSavedFilter, writeSavedFilters } from './savedFilters'

const state: AppUrlState = {
  captureId: null,
  objectiveId: 'O-1',
  priority: 'P0',
  readiness: 'blocked',
  timing: 'soon',
  search: 'release',
  status: 'started',
  surface: 'workspace',
  taskId: null,
  view: 'table',
}

beforeEach(() => window.localStorage.clear())

test('persists and compares only bounded filter coordinates', () => {
  const filter = { id: 'filter-1', name: 'Release risks', ...filterCoordinates(state) }
  writeSavedFilters([filter])
  expect(readSavedFilters()).toEqual([filter])
  expect(sameSavedFilter(filter, state)).toBe(true)
})

test('removes malformed saved filters instead of partially accepting them', () => {
  window.localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify([{ ...filterCoordinates(state), id: 'x', name: 'x', token: 'no' }]))
  expect(readSavedFilters()).toEqual([])
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBeNull()
})

test('loads a legacy v1 saved filter with safe readiness and timing defaults', () => {
  const legacy = { id: 'legacy', name: 'Legacy view', ...filterCoordinates(state) }
  delete (legacy as Partial<typeof legacy>).readiness
  delete (legacy as Partial<typeof legacy>).timing
  window.localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify([legacy]))
  expect(readSavedFilters()[0]).toMatchObject({ id: 'legacy', readiness: 'all', timing: 'all' })
})

/** Builds a stored row directly, bypassing filterCoordinates, to model old data. */
function storedRow(patch: Record<string, unknown> = {}) {
  const row: Record<string, unknown> = {
    id: 'row-1',
    name: 'Saved view',
    objectiveId: 'all',
    priority: 'all',
    readiness: 'all',
    timing: 'all',
    search: '',
    status: 'all',
    view: 'graph',
    ...patch,
  }
  return row
}

test('normalizes a legacy saved All row without the coordinate into a durable show', () => {
  window.localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify([storedRow()]))
  expect(readSavedFilters()[0]).toMatchObject({ id: 'row-1', status: 'all', doneVisibility: 'show' })
})

test('normalizes a legacy row with a non-All status to default', () => {
  window.localStorage.setItem(
    SAVED_FILTERS_KEY,
    JSON.stringify([
      storedRow({ id: 'done-row', status: 'done' }),
      storedRow({ id: 'started-row', status: 'started' }),
    ]),
  )
  const rows = readSavedFilters()
  expect(rows[0]).toMatchObject({ id: 'done-row', status: 'done', doneVisibility: 'default' })
  expect(rows[1]).toMatchObject({ id: 'started-row', doneVisibility: 'default' })
})

test('keeps a newly saved explicit default as default even when status is all', () => {
  // A stored coordinate is a supplied value, so the legacy All rule must not win.
  window.localStorage.setItem(
    SAVED_FILTERS_KEY,
    JSON.stringify([storedRow({ doneVisibility: 'default' })]),
  )
  expect(readSavedFilters()[0].doneVisibility).toBe('default')
})

test('retains explicitly stored hide and show values', () => {
  window.localStorage.setItem(
    SAVED_FILTERS_KEY,
    JSON.stringify([
      storedRow({ id: 'hide-row', status: 'started', doneVisibility: 'hide' }),
      storedRow({ id: 'show-row', status: 'started', doneVisibility: 'show' }),
    ]),
  )
  const rows = readSavedFilters()
  expect(rows[0].doneVisibility).toBe('hide')
  expect(rows[1].doneVisibility).toBe('show')
})

test('falls an invalid stored coordinate back to default without deleting the row', () => {
  window.localStorage.setItem(
    SAVED_FILTERS_KEY,
    JSON.stringify([storedRow({ id: 'weird', doneVisibility: 'nonsense' })]),
  )
  const rows = readSavedFilters()

  expect(rows).toHaveLength(1)
  expect(rows[0]).toMatchObject({ id: 'weird', doneVisibility: 'default' })
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).not.toBeNull()
})

test('still rejects unrelated unknown keys under the strict schema', () => {
  window.localStorage.setItem(
    SAVED_FILTERS_KEY,
    JSON.stringify([storedRow({ token: 'no' })]),
  )
  expect(readSavedFilters()).toEqual([])
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBeNull()
})

test('does not write storage while reading', () => {
  const stored = JSON.stringify([storedRow()])
  window.localStorage.setItem(SAVED_FILTERS_KEY, stored)
  const setItem = vi.spyOn(Storage.prototype, 'setItem')
  const removeItem = vi.spyOn(Storage.prototype, 'removeItem')
  try {
    expect(readSavedFilters()[0].doneVisibility).toBe('show')
    expect(setItem).not.toHaveBeenCalled()
    expect(removeItem).not.toHaveBeenCalled()
    // The stored bytes are unchanged: no eager migration or backfill.
    expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBe(stored)
  } finally {
    setItem.mockRestore()
    removeItem.mockRestore()
  }
})

test('an explicit write always stores a concrete coordinate, including default', () => {
  const filter = { id: 'filter-2', name: 'Fresh view', ...filterCoordinates(state) }
  expect(filter.doneVisibility).toBe('default')

  writeSavedFilters([filter])
  const raw = JSON.parse(window.localStorage.getItem(SAVED_FILTERS_KEY) ?? '[]')
  expect(raw[0].doneVisibility).toBe('default')
  expect(readSavedFilters()[0].doneVisibility).toBe('default')
})

test('a current state missing the coordinate is default, never a legacy show', () => {
  const withAllStatus: AppUrlState = { ...state, status: 'all', doneVisibility: undefined }
  expect(filterCoordinates(withAllStatus).doneVisibility).toBe('default')
})

test('filter coordinates and equality distinguish every coordinate value', () => {
  const base: AppUrlState = { ...state, doneVisibility: 'default' }
  const filter = { id: 'filter-3', name: 'Coordinate view', ...filterCoordinates(base) }

  expect(filter.doneVisibility).toBe('default')
  expect(sameSavedFilter(filter, base)).toBe(true)
  expect(sameSavedFilter(filter, { ...base, doneVisibility: 'hide' })).toBe(false)
  expect(sameSavedFilter(filter, { ...base, doneVisibility: 'show' })).toBe(false)

  const shown = { ...filter, ...filterCoordinates({ ...base, doneVisibility: 'show' }) }
  expect(sameSavedFilter(shown, { ...base, doneVisibility: 'show' })).toBe(true)
  expect(sameSavedFilter(shown, base)).toBe(false)
})

test('round-trips a saved coordinate through write and read', () => {
  for (const value of ['default', 'hide', 'show'] as const) {
    window.localStorage.clear()
    const source: AppUrlState = { ...state, doneVisibility: value }
    const filter = { id: `rt-${value}`, name: value, ...filterCoordinates(source) }
    writeSavedFilters([filter])

    const [restored] = readSavedFilters()
    expect(restored.doneVisibility).toBe(value)
    expect(sameSavedFilter(restored, source)).toBe(true)
  }
})

test('keeps the twelve-row bound with the new coordinate present', () => {
  const rows = Array.from({ length: 14 }, (_, index) => ({
    id: `bulk-${index}`,
    name: `Bulk ${index}`,
    ...filterCoordinates(state),
  }))
  const bounded = writeSavedFilters(rows)

  expect(bounded).toHaveLength(12)
  expect(bounded[0].id).toBe('bulk-2')
  expect(bounded.every((row) => row.doneVisibility === 'default')).toBe(true)
  expect(readSavedFilters()).toHaveLength(12)
})


describe('outcome coordinate rows', () => {
  test('an absent or invalid coordinate normalizes to all while the row survives', () => {
    const row = {
      id: 'F-1',
      name: 'Legacy',
      objectiveId: 'all',
      priority: 'all',
      readiness: 'all',
      timing: 'all',
      search: '',
      status: 'open',
      view: 'graph',
    } as const

    window.localStorage.setItem(
      SAVED_FILTERS_KEY,
      JSON.stringify([row, { ...row, id: 'F-2', outcomeFilter: { kind: 'pair', objectiveId: '' } }]),
    )

    const rows = readSavedFilters()

    expect(rows).toHaveLength(2)
    expect(rows[0].outcomeFilter).toEqual({ kind: 'all' })
    expect(rows[1].outcomeFilter).toEqual({ kind: 'all' })
    expect(rows[1].id).toBe('F-2')
  })

  test('a typed pair coordinate is stored and compared exactly', () => {
    const coordinate = { kind: 'pair', objectiveId: 'O-A::B', keyResultId: 'KR-X' } as const
    const stored = writeSavedFilters([{
      id: 'F-3',
      name: 'Outcome',
      objectiveId: 'all',
      priority: 'all',
      readiness: 'all',
      timing: 'all',
      search: '',
      status: 'open',
      view: 'graph',
      outcomeFilter: coordinate,
    }])

    expect(stored[0].outcomeFilter).toEqual(coordinate)
    expect(readSavedFilters()[0].outcomeFilter).toEqual(coordinate)
  })
})

function typedOutcomeRow(patch: Record<string, unknown> = {}) {
  return {
    id: 'outcome-ok',
    name: 'Outcome view',
    objectiveId: 'all',
    priority: 'all',
    readiness: 'all',
    timing: 'all',
    search: '',
    status: 'open',
    view: 'graph',
    outcomeFilter: { kind: 'pair', objectiveId: 'O-A::B', keyResultId: 'KR-X' },
    ...patch,
  }
}

function validLegacyRow(patch: Record<string, unknown> = {}) {
  const row: Record<string, unknown> = {
    id: 'legacy-ok',
    name: 'Legacy view',
    objectiveId: 'all',
    priority: 'all',
    search: '',
    status: 'all',
    view: 'graph',
    ...patch,
  }
  return row
}

test('keeps valid saved views when one stored record is malformed', () => {
  const raw = JSON.stringify([
    validLegacyRow(),
    storedRow({ id: 'bad', token: 'no' }),
    typedOutcomeRow(),
  ])
  window.localStorage.setItem(SAVED_FILTERS_KEY, raw)
  const setItem = vi.spyOn(Storage.prototype, 'setItem')
  const removeItem = vi.spyOn(Storage.prototype, 'removeItem')
  try {
    const rows = readSavedFilters()
    expect(rows).toHaveLength(2)
    expect(rows.map((row) => row.id)).toEqual(['legacy-ok', 'outcome-ok'])
    expect(rows[0]).toMatchObject({
      id: 'legacy-ok',
      name: 'Legacy view',
      readiness: 'all',
      timing: 'all',
      doneVisibility: 'show',
      outcomeFilter: { kind: 'all' },
    })
    expect(rows[1]).toMatchObject({
      id: 'outcome-ok',
      outcomeFilter: { kind: 'pair', objectiveId: 'O-A::B', keyResultId: 'KR-X' },
    })
    expect(setItem).not.toHaveBeenCalled()
    expect(removeItem).not.toHaveBeenCalled()
    expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBe(raw)
  } finally {
    setItem.mockRestore()
    removeItem.mockRestore()
  }
})

test('preserves a valid row that follows an invalid sibling', () => {
  window.localStorage.setItem(
    SAVED_FILTERS_KEY,
    JSON.stringify([
      storedRow({ id: 'bad-first', token: 'no' }),
      storedRow({ id: 'good-last' }),
    ]),
  )
  const rows = readSavedFilters()
  expect(rows).toHaveLength(1)
  expect(rows[0]).toMatchObject({ id: 'good-last', doneVisibility: 'show' })
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).not.toBeNull()
})

test('removes a stored empty array because no valid row survives', () => {
  window.localStorage.setItem(SAVED_FILTERS_KEY, '[]')
  expect(readSavedFilters()).toEqual([])
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBeNull()
})

test('clears storage when every stored record is invalid', () => {
  window.localStorage.setItem(
    SAVED_FILTERS_KEY,
    JSON.stringify([
      storedRow({ id: 'bad-a', token: 'no' }),
      storedRow({ id: 'bad-b', extra: true }),
    ]),
  )
  expect(readSavedFilters()).toEqual([])
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBeNull()
})

test('clears storage for invalid JSON, a non-array root, and an oversized array', () => {
  window.localStorage.setItem(SAVED_FILTERS_KEY, '{')
  expect(readSavedFilters()).toEqual([])
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBeNull()

  window.localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify({ id: 'not-an-array' }))
  expect(readSavedFilters()).toEqual([])
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBeNull()

  const oversized = Array.from({ length: 13 }, (_, index) => storedRow({ id: `over-${index}` }))
  window.localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify(oversized))
  expect(readSavedFilters()).toEqual([])
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBeNull()
})

test('invalid optional coordinates still normalize instead of quarantining the row', () => {
  const raw = JSON.stringify([
    storedRow({ id: 'opt-done', doneVisibility: 'nonsense' }),
    storedRow({ id: 'bad', token: 'no' }),
    storedRow({ id: 'opt-outcome', outcomeFilter: { kind: 'pair', objectiveId: '' } }),
  ])
  window.localStorage.setItem(SAVED_FILTERS_KEY, raw)
  const rows = readSavedFilters()
  expect(rows.map((row) => row.id)).toEqual(['opt-done', 'opt-outcome'])
  expect(rows[0].doneVisibility).toBe('default')
  expect(rows[1].outcomeFilter).toEqual({ kind: 'all' })
  expect(window.localStorage.getItem(SAVED_FILTERS_KEY)).toBe(raw)
})

test('rejects an invalid requested write instead of persisting siblings', () => {
  writeSavedFilters([storedRow({ id: 'kept' }) as never])
  expect(() => writeSavedFilters([
    storedRow({ id: 'ok' }) as never,
    storedRow({ id: 'bad', token: 'no' }) as never,
  ])).toThrow()
  expect(readSavedFilters()).toHaveLength(1)
  expect(readSavedFilters()[0].id).toBe('kept')
})
