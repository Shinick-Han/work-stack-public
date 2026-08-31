import { beforeEach, expect, test } from 'vitest'
import { TABLE_PREFERENCES_KEY, readTablePreferences, writeTablePreferences } from './tablePreferences'

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
