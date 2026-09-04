import { describe, expect, test } from 'vitest'
import { dateLabel, localToday, monthCells, moveDay, moveMonth, parseIsoDate, weekday } from './dateInputModel'

describe('civil dates', () => {
  test.each(['2024-02-29', '2000-02-29', '0001-01-01', '0099-12-31', '9999-12-31'])('accepts canonical %s without a timezone roundtrip', (value) => {
    expect(parseIsoDate(value)).not.toBeNull()
  })
  test.each(['', '0000-01-01', '10000-01-01', '2025-02-29', '1900-02-29', '2024-04-31', '2024-13-01', '2024-00-10', '2024-01-00', '2024-1-01', '2024-01-01T00:00:00Z', ' 2024-01-01'])('rejects %s', (value) => {
    expect(parseIsoDate(value)).toBeNull()
  })
  test('moves across leap, year and DST boundaries without changing civil intent', () => {
    expect(moveDay('2024-02-28', 1)).toBe('2024-02-29')
    expect(moveDay('2024-02-29', 1)).toBe('2024-03-01')
    expect(moveDay('2025-01-01', -1)).toBe('2024-12-31')
    expect(moveDay('2024-03-10', 1)).toBe('2024-03-11')
    expect(moveDay('2024-11-03', -1)).toBe('2024-11-02')
    expect(moveDay('0001-01-01', -1)).toBe('0001-01-01')
    expect(moveDay('9999-12-31', 1)).toBe('9999-12-31')
  })
  test('clamps month navigation to its last valid day, including early years', () => {
    expect(moveMonth('2024-01-31', 1)).toBe('2024-02-29')
    expect(moveMonth('2025-03-31', -1)).toBe('2025-02-28')
    expect(moveMonth('0099-12-31', 1)).toBe('0100-01-31')
    expect(moveMonth('0001-01-01', -1)).toBe('0001-01-01')
  })
  test('owns English calendar labels and the Gregorian weekday layout', () => {
    expect(dateLabel('2024-02-29')).toBe('February 29, 2024')
    expect(weekday({ year: 1, month: 1, day: 1 })).toBe(1)
    const cells = monthCells('2024-02-29').flat()
    expect(cells.indexOf('2024-02-01')).toBe(4)
    expect(cells.filter(Boolean)).toHaveLength(29)
    expect(cells).not.toContain('2024-02-30')
  })
  test('Today uses local clock components even where UTC is a different day', () => {
    const localClock = { getFullYear: () => 2024, getMonth: () => 0, getDate: () => 2 } as Date
    expect(localToday(localClock)).toBe('2024-01-02')
    expect(localToday(new Date(2024, 1, 29, 23, 59))).toBe('2024-02-29')
  })
})
