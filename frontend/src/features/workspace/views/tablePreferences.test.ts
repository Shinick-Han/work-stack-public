import { beforeEach, expect, test } from 'vitest'
import { TABLE_ORDER_CAP } from './tableOrdering'
import {
  EMPTY_TABLE_LOCAL_VIEW,
  TABLE_PREFERENCES_KEY,
  parseTableLocalViewData,
  readTablePreferences,
  seedTableLocalViewData,
  writeTablePreferences,
} from './tablePreferences'

beforeEach(() => window.localStorage.clear())

test('round-trips only the bounded Table display coordinates', () => {
  const preferences = { density: 'compact' as const, descending: true, sortField: 'due' as const }
  writeTablePreferences(preferences)
  expect(readTablePreferences()).toEqual(preferences)
})

test('removes malformed or expanded preference records and returns safe defaults', () => {
  window.localStorage.setItem(TABLE_PREFERENCES_KEY, JSON.stringify({
    density: 'compact', descending: false, sortField: 'id', token: 'must-not-survive',
  }))
  expect(readTablePreferences()).toEqual({ density: 'comfortable', descending: false, sortField: 'id' })
  expect(window.localStorage.getItem(TABLE_PREFERENCES_KEY)).toBeNull()
})

test('rejects a Manual sort in the legacy global record so it cannot seed a named mode', () => {
  window.localStorage.setItem(TABLE_PREFERENCES_KEY, JSON.stringify({
    density: 'compact', descending: false, sortField: 'manual',
  }))
  expect(readTablePreferences()).toEqual({ density: 'comfortable', descending: false, sortField: 'id' })
  expect(window.localStorage.getItem(TABLE_PREFERENCES_KEY)).toBeNull()
})

test('seeds per-workspace Table data from a valid legacy record without deleting it', () => {
  const legacy = { density: 'compact' as const, descending: true, sortField: 'status' as const }
  writeTablePreferences(legacy)
  expect(seedTableLocalViewData()).toEqual({ ...legacy, order: [] })
  expect(window.localStorage.getItem(TABLE_PREFERENCES_KEY)).toBeTruthy()
})

test('fails closed when legacy storage get throws without retrying remove', () => {
  const remove = vi.spyOn(Storage.prototype, 'removeItem')
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
    throw new DOMException('Denied', 'SecurityError')
  })
  expect(readTablePreferences()).toEqual({ density: 'comfortable', descending: false, sortField: 'id' })
  expect(remove).not.toHaveBeenCalled()
})

test('fails closed when legacy storage writes are disabled', () => {
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('Denied', 'SecurityError')
  })
  expect(writeTablePreferences({ density: 'compact', descending: true, sortField: 'due' })).toEqual({
    density: 'compact',
    descending: true,
    sortField: 'due',
  })
})

test('parses a bounded Table local-view payload and rejects unknown keys, duplicates, and oversize orders', () => {
  expect(parseTableLocalViewData({
    density: 'compact',
    descending: true,
    sortField: 'manual',
    order: ['T-1', 'T-2'],
  })).toEqual({
    density: 'compact',
    descending: true,
    sortField: 'manual',
    order: ['T-1', 'T-2'],
  })
  expect(parseTableLocalViewData({ ...EMPTY_TABLE_LOCAL_VIEW, extra: true })).toBeNull()
  expect(parseTableLocalViewData({
    density: 'comfortable',
    descending: false,
    sortField: 'id',
    order: ['T-1', 'T-1'],
  })).toBeNull()
  expect(parseTableLocalViewData({
    density: 'comfortable',
    descending: false,
    sortField: 'id',
    order: Array.from({ length: TABLE_ORDER_CAP + 1 }, (_, index) => `T-${index}`),
  })).toBeNull()
  expect(parseTableLocalViewData({
    density: 'comfortable',
    descending: false,
    sortField: 'id',
    order: Array.from({ length: TABLE_ORDER_CAP }, (_, index) => `T-${index}`),
  })?.order).toHaveLength(TABLE_ORDER_CAP)
})
