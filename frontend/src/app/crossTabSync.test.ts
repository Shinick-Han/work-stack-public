import { beforeEach, expect, test, vi } from 'vitest'
import { CROSS_TAB_STORAGE_KEY, publishPlanningChange, subscribePlanningChanges } from './crossTabSync'

beforeEach(() => {
  window.localStorage.clear()
})

test('publishes a content-free versioned refresh hint', () => {
  publishPlanningChange()
  const value = JSON.parse(window.localStorage.getItem(CROSS_TAB_STORAGE_KEY) ?? '{}')
  expect(Object.keys(value).sort()).toEqual(['at', 'nonce', 'source', 'version'])
  expect(value.version).toBe(1)
  expect(JSON.stringify(value)).not.toMatch(/task|capture|reply|recipient|locator/i)
})

test('accepts each another-tab nonce once and ignores malformed input', () => {
  const onChange = vi.fn()
  const unsubscribe = subscribePlanningChanges(onChange)
  const duplicate = new StorageEvent('storage', {
    key: CROSS_TAB_STORAGE_KEY,
    newValue: JSON.stringify({ at: Date.now(), nonce: 'n-1', source: 'another-tab', version: 1 }),
  })
  window.dispatchEvent(duplicate)
  window.dispatchEvent(new StorageEvent('storage', {
    key: CROSS_TAB_STORAGE_KEY,
    newValue: JSON.stringify({ at: Date.now(), nonce: 'n-1', source: 'another-tab', version: 1 }),
  }))
  window.dispatchEvent(new StorageEvent('storage', {
    key: CROSS_TAB_STORAGE_KEY,
    newValue: JSON.stringify({ at: Date.now(), nonce: 'n-2', source: 'another-tab', version: 1 }),
  }))
  window.dispatchEvent(new StorageEvent('storage', {
    key: CROSS_TAB_STORAGE_KEY,
    newValue: '{bad json',
  }))
  expect(onChange).toHaveBeenCalledTimes(2)
  unsubscribe()
})
