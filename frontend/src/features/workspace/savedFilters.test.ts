import { beforeEach, expect, test } from 'vitest'
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
