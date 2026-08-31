import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useLocalToday } from './useLocalToday'

function localDate(
  year: number,
  month: number,
  day: number,
  hour = 0,
  minute = 0,
  second = 0,
  millisecond = 0,
) {
  return new Date(year, month - 1, day, hour, minute, second, millisecond)
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

test('reads the local calendar date on mount', () => {
  vi.setSystemTime(localDate(2026, 8, 29, 9, 30))
  const { result } = renderHook(() => useLocalToday())

  expect(result.current).toBe('2026-08-29')
})

test('updates after local midnight and schedules the following midnight', () => {
  vi.setSystemTime(localDate(2026, 8, 29, 23, 59, 59, 900))
  const { result } = renderHook(() => useLocalToday())

  expect(result.current).toBe('2026-08-29')
  expect(vi.getTimerCount()).toBe(1)
  act(() => vi.advanceTimersByTime(126))
  expect(result.current).toBe('2026-08-30')
  expect(vi.getTimerCount()).toBe(1)
})

test('recomputes on hidden-to-visible and reschedules even when the date is unchanged', () => {
  vi.setSystemTime(localDate(2026, 8, 29, 9))
  const visibility = vi.spyOn(document, 'visibilityState', 'get')
  visibility.mockReturnValue('hidden')
  const { result } = renderHook(() => useLocalToday())
  expect(result.current).toBe('2026-08-29')

  vi.setSystemTime(localDate(2026, 8, 30, 9))
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(result.current).toBe('2026-08-29')

  visibility.mockReturnValue('visible')
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(result.current).toBe('2026-08-30')
  expect(vi.getTimerCount()).toBe(1)

  act(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(result.current).toBe('2026-08-30')
  expect(vi.getTimerCount()).toBe(1)
})

test('cleans up its timer and visibility listener', () => {
  vi.setSystemTime(localDate(2026, 8, 29, 12))
  const remove = vi.spyOn(document, 'removeEventListener')
  const { unmount } = renderHook(() => useLocalToday())

  expect(vi.getTimerCount()).toBe(1)
  unmount()

  expect(vi.getTimerCount()).toBe(0)
  expect(remove).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
})

test('never fetches or refetches data', () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  vi.setSystemTime(localDate(2026, 8, 29, 23, 59, 59, 900))
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  renderHook(() => useLocalToday())

  act(() => vi.advanceTimersByTime(126))
  act(() => document.dispatchEvent(new Event('visibilitychange')))

  expect(fetch).not.toHaveBeenCalled()
})
