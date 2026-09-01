import type { Task, TaskPriority, TaskStatus } from '../../domain/types'
import {
  blockingDependenciesFromIndex,
  indexDependencyTasks,
  type TaskBlocker,
} from '../../domain/taskRelationships'

const MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1_000
const CANONICAL_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/

export const FOCUS_REASON_RANK = {
  invalid_due: 0,
  overdue: 1,
  due_today: 2,
  due_soon: 3,
  high_priority: 4,
  in_progress: 5,
} as const

export type FocusReasonKind = keyof typeof FOCUS_REASON_RANK

export interface CivilDate {
  iso: string
  year: number
  month: number
  day: number
  ordinal: number
}

export interface FocusReason {
  kind: FocusReasonKind
  label: string
  rank: number
  days?: number
}

export interface FocusCandidate {
  task: Task
  reasons: FocusReason[]
  blockers: TaskBlocker[]
}

export interface FocusProjection {
  today: string
  activeCount: number
  focusCount: number
  actionableCount: number
  blockedCount: number
  candidates: FocusCandidate[]
}

export type FocusModelErrorCode =
  | 'duplicate_task_id'
  | 'invalid_calendar_date'
  | 'invalid_day_offset'
  | 'invalid_today'

/**
 * A bounded projection error that callers can turn into an inline Focus error state.
 * Duplicate IDs deliberately fail instead of silently hiding one of the tasks.
 */
export class FocusModelError extends Error {
  readonly code: FocusModelErrorCode

  constructor(code: FocusModelErrorCode, message: string) {
    super(message)
    this.name = 'FocusModelError'
    this.code = code
  }
}

function pad(value: number, width = 2) {
  return String(value).padStart(width, '0')
}

function formatCivilDate(year: number, month: number, day: number) {
  if (year < 1 || year > 9_999) {
    throw new FocusModelError(
      'invalid_calendar_date',
      'calendar date year must be between 0001 and 9999',
    )
  }
  return `${pad(year, 4)}-${pad(month)}-${pad(day)}`
}

function utcDateFromComponents(year: number, month: number, day: number) {
  const date = new Date(Date.UTC(year, month - 1, day))
  // Date.UTC treats 0..99 as 1900..1999. Restore the requested civil year before
  // round-trip validation and ordinal calculation.
  if (year >= 0 && year <= 99) date.setUTCFullYear(year)
  return date
}

export function parseCanonicalCivilDate(value: unknown): CivilDate | null {
  if (typeof value !== 'string') return null
  const match = CANONICAL_DATE_RE.exec(value)
  if (!match) return null

  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  if (year < 1 || year > 9_999) return null

  const date = utcDateFromComponents(year, month, day)
  if (
    Number.isNaN(date.getTime())
    || date.getUTCFullYear() !== year
    || date.getUTCMonth() + 1 !== month
    || date.getUTCDate() !== day
  ) {
    return null
  }

  return Object.freeze({
    iso: value,
    year,
    month,
    day,
    ordinal: date.getTime() / MILLISECONDS_PER_DAY,
  })
}

function requireCivilDate(value: string, field: string) {
  const parsed = parseCanonicalCivilDate(value)
  if (!parsed) {
    throw new FocusModelError(
      'invalid_calendar_date',
      `${field} must be a canonical YYYY-MM-DD calendar date`,
    )
  }
  return parsed
}

export function localIsoDate(date = new Date()) {
  if (Number.isNaN(date.getTime())) {
    throw new FocusModelError('invalid_calendar_date', 'local date is invalid')
  }
  return formatCivilDate(date.getFullYear(), date.getMonth() + 1, date.getDate())
}

export function addCalendarDays(value: string, days: number) {
  if (!Number.isInteger(days)) {
    throw new FocusModelError('invalid_day_offset', 'calendar day offset must be an integer')
  }
  const parsed = requireCivilDate(value, 'date')
  const result = new Date((parsed.ordinal + days) * MILLISECONDS_PER_DAY)
  return formatCivilDate(
    result.getUTCFullYear(),
    result.getUTCMonth() + 1,
    result.getUTCDate(),
  )
}

/** Returns `to - from` in civil calendar days, independent of DST. */
export function civilDayDistance(from: string, to: string) {
  const start = requireCivilDate(from, 'from')
  const end = requireCivilDate(to, 'to')
  return end.ordinal - start.ordinal
}

function reason(
  kind: FocusReasonKind,
  label: string,
  days?: number,
): FocusReason {
  return {
    kind,
    label,
    rank: FOCUS_REASON_RANK[kind],
    ...(days === undefined ? {} : { days }),
  }
}

function pluralizedDay(count: number) {
  return `${count} ${count === 1 ? 'day' : 'days'}`
}

function dueReasonFromDates(
  dueValue: string | null,
  today: CivilDate,
  dueSoonEnd: CivilDate,
) {
  if (dueValue === null) return null
  const due = parseCanonicalCivilDate(dueValue)
  if (!due) return reason('invalid_due', 'Due date needs review')
  if (due.ordinal < today.ordinal) {
    const days = today.ordinal - due.ordinal
    return reason('overdue', `${pluralizedDay(days)} overdue`, days)
  }
  if (due.ordinal === today.ordinal) return reason('due_today', 'Due today', 0)
  if (due.ordinal <= dueSoonEnd.ordinal) {
    const days = due.ordinal - today.ordinal
    return reason('due_soon', `Due in ${pluralizedDay(days)}`, days)
  }
  return null
}

/** Shared local-calendar due projection for Focus and read-only planning views. */
export function dueReasonForDate(dueValue: string | null, todayValue: string) {
  const today = parseCanonicalCivilDate(todayValue)
  if (!today) {
    throw new FocusModelError(
      'invalid_today',
      'today must be a canonical YYYY-MM-DD calendar date',
    )
  }
  const dueSoonEnd = requireCivilDate(addCalendarDays(today.iso, 7), 'due soon boundary')
  return dueReasonFromDates(dueValue, today, dueSoonEnd)
}

function reasonsForTask(task: Task, today: CivilDate, dueSoonEnd: CivilDate) {
  const reasons: FocusReason[] = []

  const dueReason = dueReasonFromDates(task.due, today, dueSoonEnd)
  if (dueReason) reasons.push(dueReason)

  if (task.priority === 'P0' || task.priority === 'P1') {
    reasons.push(reason('high_priority', task.priority))
  }
  if (task.status === 'started') {
    reasons.push(reason('in_progress', 'In progress'))
  }

  return reasons.sort((left, right) => left.rank - right.rank)
}

const PRIORITY_RANK: Record<TaskPriority, number> = {
  P0: 0,
  P1: 1,
  P2: 2,
  P3: 3,
}

const STATUS_RANK: Record<TaskStatus, number> = {
  started: 0,
  open: 1,
  done: 2,
  dropped: 3,
}

function asciiCompare(left: string, right: string) {
  if (left === right) return 0
  return left < right ? -1 : 1
}

export function buildFocusProjection(
  tasks: readonly Task[],
  todayValue: string,
): FocusProjection {
  const today = parseCanonicalCivilDate(todayValue)
  if (!today) {
    throw new FocusModelError(
      'invalid_today',
      'today must be a canonical YYYY-MM-DD calendar date',
    )
  }
  const dueSoonEnd = requireCivilDate(addCalendarDays(today.iso, 7), 'due soon boundary')

  const seen = new Set<string>()
  for (const task of tasks) {
    if (seen.has(task.id)) {
      throw new FocusModelError(
        'duplicate_task_id',
        `Focus projection cannot safely represent duplicate Task ID ${task.id}`,
      )
    }
    seen.add(task.id)
  }

  const activeTasks = tasks.filter(
    (task) => task.status === 'open' || task.status === 'started',
  )
  const dependencyIndex = indexDependencyTasks(tasks)
  const sortableCandidates = activeTasks.flatMap((task) => {
    const reasons = reasonsForTask(task, today, dueSoonEnd)
    if (!reasons.length) return []
    const blockers = blockingDependenciesFromIndex(dependencyIndex, task)
    return [{
      task,
      reasons,
      blockers,
      blockedRank: blockers.length ? 1 : 0,
      urgencyRank: reasons[0].rank,
      dueOrdinal: parseCanonicalCivilDate(task.due)?.ordinal ?? Number.POSITIVE_INFINITY,
    }]
  })

  sortableCandidates.sort((left, right) => (
    left.blockedRank - right.blockedRank
    || left.urgencyRank - right.urgencyRank
    || left.dueOrdinal - right.dueOrdinal
    || PRIORITY_RANK[left.task.priority] - PRIORITY_RANK[right.task.priority]
    || STATUS_RANK[left.task.status] - STATUS_RANK[right.task.status]
    || asciiCompare(left.task.id, right.task.id)
  ))

  const candidates = sortableCandidates.map(({ task, reasons, blockers }) => ({ task, reasons, blockers }))
  const blockedCount = candidates.filter((candidate) => candidate.blockers.length).length
  return {
    today: today.iso,
    activeCount: activeTasks.length,
    focusCount: candidates.length,
    actionableCount: candidates.length - blockedCount,
    blockedCount,
    candidates,
  }
}
