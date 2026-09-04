export interface CivilDate {
  year: number
  month: number
  day: number
}

export const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
] as const
export const WEEKDAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'] as const

export function daysInMonth(year: number, month: number): number {
  if (month === 2) return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28
  return [4, 6, 9, 11].includes(month) ? 30 : 31
}

export function parseIsoDate(value: string): CivilDate | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null
  const [, y, m, d] = match
  const year = Number(y), month = Number(m), day = Number(d)
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return null
  return { year, month, day }
}

export function formatIsoDate({ year, month, day }: CivilDate): string {
  return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

/** Only Today reads the clock; selected dates never cross a timezone boundary. */
export function localToday(now = new Date()): string {
  return formatIsoDate({ year: now.getFullYear(), month: now.getMonth() + 1, day: now.getDate() })
}

export function dateLabel(value: string): string {
  const date = parseIsoDate(value)
  return date ? `${MONTH_NAMES[date.month - 1]} ${date.day}, ${String(date.year).padStart(4, '0')}` : value
}

export function isDateAllowed(value: string, min?: string, max?: string): boolean {
  return parseIsoDate(value) !== null && (!min || value >= min) && (!max || value <= max)
}

export function clampDate(value: string, min?: string, max?: string): string {
  if (min && value < min) return min
  if (max && value > max) return max
  return value
}

/** Gregorian weekday, Sunday=0, using integers (including years 0001..0099). */
export function weekday({ year, month, day }: CivilDate): number {
  const previousYear = year - 1
  let ordinal = 365 * previousYear + Math.floor(previousYear / 4) - Math.floor(previousYear / 100) + Math.floor(previousYear / 400) + day
  for (let m = 1; m < month; m++) ordinal += daysInMonth(year, m)
  return ordinal % 7
}

export function moveDay(value: string, amount: number): string {
  const parsed = parseIsoDate(value)
  if (!parsed || !Number.isInteger(amount) || Math.abs(amount) > 366) throw new RangeError('Invalid civil date or day offset')
  let { year, month, day } = parsed
  for (let step = 0; step < Math.abs(amount); step++) {
    if (amount > 0) {
      if (year === 9999 && month === 12 && day === 31) break
      if (++day > daysInMonth(year, month)) { day = 1; if (++month > 12) { month = 1; year++ } }
    } else {
      if (year === 1 && month === 1 && day === 1) break
      if (--day < 1) { if (--month < 1) { month = 12; year-- }; day = daysInMonth(year, month) }
    }
  }
  return formatIsoDate({ year, month, day })
}

export function moveMonth(value: string, amount: number): string {
  const date = parseIsoDate(value)
  if (!date || !Number.isInteger(amount)) throw new RangeError('Invalid civil date or month offset')
  const index = Math.max(0, Math.min(9999 * 12 - 1, (date.year - 1) * 12 + date.month - 1 + amount))
  const year = Math.floor(index / 12) + 1, month = index % 12 + 1
  return formatIsoDate({ year, month, day: Math.min(date.day, daysInMonth(year, month)) })
}

export function monthCells(value: string): (string | null)[][] {
  const date = parseIsoDate(value)
  if (!date) throw new RangeError('Invalid civil date')
  const cells: (string | null)[] = Array(weekday({ ...date, day: 1 })).fill(null)
  for (let day = 1; day <= daysInMonth(date.year, date.month); day++) cells.push(formatIsoDate({ ...date, day }))
  while (cells.length % 7) cells.push(null)
  return Array.from({ length: cells.length / 7 }, (_, i) => cells.slice(i * 7, i * 7 + 7))
}
