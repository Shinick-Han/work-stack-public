import { expect, test } from 'vitest'
import {
  compareRfc3339Instants,
  parseRfc3339Instant,
  rfc3339WindowIsValid,
} from './knowledgeRequestTime'

function instant(value: string) {
  const parsed = parseRfc3339Instant(value)
  if (!parsed) throw new Error(`expected ${value} to parse`)
  return parsed
}

test('happy calendar dates and offsets parse, including leap February', () => {
  expect(instant('2026-09-08T09:00:00Z').utcSeconds).toBe(1788858000n)
  expect(instant('2026-09-08T18:00:00+09:00').utcSeconds).toBe(1788858000n)
  expect(instant('2024-02-29T23:59:59-00:00').utcSeconds).toBe(1709251199n)
  expect(instant('2026-01-01T00:00:00-23:59').utcSeconds).toBe(1767311940n)
  expect(instant('2000-02-29T00:00:00Z')).toMatchObject({ fraction: '' })
  expect(instant('2026-08-29T08:00:00.1Z').fraction).toBe('1')
  expect(compareRfc3339Instants(
    instant('2026-08-29T08:00:00.1Z'),
    instant('2026-08-29T17:00:00.100+09:00'),
  )).toBe(0)
})

test('impossible days, non-leap February and malformed offsets refuse', () => {
  for (const value of [
    '2026-02-30T09:00:00Z',
    '2026-02-29T08:00:00Z',
    '1900-02-29T00:00:00Z',
    '2026-04-31T09:00:00Z',
    '2026-00-01T00:00:00Z',
    '2026-13-01T00:00:00Z',
    '2026-01-32T00:00:00Z',
    '2026-08-29T24:00:00Z',
    '2026-08-29T08:00:60Z',
    '2026-08-29T08:00:00+24:00',
    '2026-08-29T08:00:00+09:60',
    '2026-08-29T08:00:00.Z',
    '2026-08-29 08:00:00Z',
    '2026-08-29T08:00:00',
    '0000-01-01T00:00:00Z',
    '0001-01-01T00:00:00+00:01',
  ]) {
    expect(parseRfc3339Instant(value), value).toBeNull()
  }
})

test('a sub-millisecond window stays ordered when milliseconds coincide', () => {
  const requested = instant('2026-09-08T09:00:00.000000001Z')
  const expires = instant('2026-09-08T09:00:00.000000002Z')
  expect(requested.unixMs).toBe(expires.unixMs)
  expect(requested.unixMs).toBe(Date.parse('2026-09-08T09:00:00.000Z'))
  expect(rfc3339WindowIsValid(requested, expires, 300)).toBe(true)
  expect(rfc3339WindowIsValid(expires, requested, 300)).toBe(false)
  expect(
    rfc3339WindowIsValid(instant('2026-09-08T09:00:00.500Z'), instant('2026-09-08T09:05:00.500Z'), 300),
  ).toBe(true)
  expect(
    rfc3339WindowIsValid(instant('2026-09-08T09:00:00.500Z'), instant('2026-09-08T09:05:00.600Z'), 300),
  ).toBe(false)
})
