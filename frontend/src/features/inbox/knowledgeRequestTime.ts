/**
 * Strict RFC3339 instants matching `workstack.capture.parse_rfc3339`.
 *
 * The grammar is Capture Packet v1's: a civil date-time, optional fractional
 * seconds of one or more digits, and a numeric offset or `Z`. Impossible days
 * (31 April, 29 February on a non-leap year) are refused. Comparison keeps the
 * stored fraction string, so a 1 ns window stays ordered even when both
 * instants truncate to the same JavaScript millisecond. The injectable editor
 * clock is still whole milliseconds; this helper never treats `Date.parse` as
 * validation.
 */

export const MAX_TIMESTAMP_CHARS = 64

const RFC3339_RE =
  /^(\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)(?:\.(\d+))?(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/

const UNIX_SECONDS_YEAR_1 = -62135596800n
const UNIX_SECONDS_YEAR_9999_END = 253402300799n
const DAYS_IN_MONTH = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

export interface Rfc3339Instant {
  /** Whole UTC seconds since the Unix epoch. */
  utcSeconds: bigint
  /** Fractional seconds with trailing zeros stripped, as the Python parser stores. */
  fraction: string
  /** Instant truncated toward zero to whole milliseconds, for the JS clock. */
  unixMs: number
}

function isLeapYear(year: number) {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
}

function daysInMonth(year: number, month: number) {
  if (month === 2) return isLeapYear(year) ? 29 : 28
  return DAYS_IN_MONTH[month]
}

function isCivilDate(year: number, month: number, day: number) {
  if (year < 1 || year > 9999) return false
  if (month < 1 || month > 12) return false
  return day >= 1 && day <= daysInMonth(year, month)
}

function daysSinceUnixEpoch(year: number, month: number, day: number) {
  let days = 0
  if (year >= 1970) {
    for (let y = 1970; y < year; y += 1) days += isLeapYear(y) ? 366 : 365
  } else {
    for (let y = year; y < 1970; y += 1) days -= isLeapYear(y) ? 366 : 365
  }
  for (let monthIndex = 1; monthIndex < month; monthIndex += 1) {
    days += daysInMonth(year, monthIndex)
  }
  return days + (day - 1)
}

function offsetSeconds(offset: string) {
  if (offset === 'Z') return 0
  const sign = offset.startsWith('-') ? -1 : 1
  return sign * (Number(offset.slice(1, 3)) * 3600 + Number(offset.slice(4, 6)) * 60)
}

function fractionMs(fraction: string) {
  return Number((fraction + '000').slice(0, 3))
}

export function parseRfc3339Instant(value: unknown): Rfc3339Instant | null {
  if (typeof value !== 'string' || value.length === 0 || value.length > MAX_TIMESTAMP_CHARS) {
    return null
  }
  const match = RFC3339_RE.exec(value)
  if (!match) return null
  const year = Number(value.slice(0, 4))
  const month = Number(value.slice(5, 7))
  const day = Number(value.slice(8, 10))
  const hour = Number(value.slice(11, 13))
  const minute = Number(value.slice(14, 16))
  const second = Number(value.slice(17, 19))
  if (!isCivilDate(year, month, day)) return null
  const fraction = (match[2] ?? '').replace(/0+$/, '')
  const utcSeconds =
    BigInt(daysSinceUnixEpoch(year, month, day)) * 86400n +
    BigInt(hour * 3600 + minute * 60 + second - offsetSeconds(match[3]))
  if (utcSeconds < UNIX_SECONDS_YEAR_1 || utcSeconds > UNIX_SECONDS_YEAR_9999_END) return null
  const unixMs = Number(utcSeconds) * 1000 + fractionMs(fraction)
  if (!Number.isFinite(unixMs)) return null
  return { fraction, unixMs, utcSeconds }
}

export function compareRfc3339Instants(left: Rfc3339Instant, right: Rfc3339Instant) {
  if (left.utcSeconds !== right.utcSeconds) return left.utcSeconds < right.utcSeconds ? -1 : 1
  if (left.fraction === right.fraction) return 0
  return left.fraction < right.fraction ? -1 : 1
}

/**
 * Strictly positive and at most `maxActiveSeconds` later, measured the way
 * `workstack.knowledge_request._window` measures: same-second ties use the
 * requested fraction as the inclusive ceiling.
 */
export function rfc3339WindowIsValid(
  requested: Rfc3339Instant,
  expires: Rfc3339Instant,
  maxActiveSeconds: number,
) {
  if (compareRfc3339Instants(requested, expires) >= 0) return false
  const limit: Rfc3339Instant = {
    fraction: requested.fraction,
    unixMs: requested.unixMs,
    utcSeconds: requested.utcSeconds + BigInt(maxActiveSeconds),
  }
  return compareRfc3339Instants(expires, limit) <= 0
}
