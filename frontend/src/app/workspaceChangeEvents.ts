/** Only the admitted committed variant is a UI signal; it carries no user prose. */
export const WORKSPACE_CHANGE_EVENT = 'workstack.change.v1'

export interface CheckpointCommittedEvent {
  event_id: number
  kind: 'agent.checkpoint.committed'
  workspace_uid: string
  task_id: string
  date: string
  checkpoint_id: string
  done_count: number
  next_count: number
  blocker_count: number
  first_for_task: boolean
  origin: 'agent-cli-v1'
  replayed: false
}

const fields = [
  'event_id', 'kind', 'workspace_uid', 'task_id', 'date', 'checkpoint_id',
  'done_count', 'next_count', 'blocker_count', 'first_for_task', 'origin', 'replayed',
] as const

function integer(value: unknown, minimum: number, maximum: number): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum && value <= maximum
}

function matches(value: unknown, pattern: RegExp): value is string {
  // JS's $ also matches before a final newline; require the complete matched value.
  return typeof value === 'string' && pattern.exec(value)?.[0] === value
}

function canonicalDate(value: unknown): value is string {
  if (!matches(value, /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/)) return false
  const [year, month, day] = value.split('-').map(Number)
  if (year < 1 || month < 1 || month > 12 || day < 1) return false
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  return day <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
}

function identifiers(value: Record<string, unknown>): boolean {
  return matches(value.workspace_uid, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
    && matches(value.task_id, /^T-[0-9]{4,}$/)
    && matches(value.checkpoint_id, /^CP-[0-9a-f]{64}$/)
    && canonicalDate(value.date)
}

function counts(value: Record<string, unknown>): boolean {
  return integer(value.done_count, 0, 20) && integer(value.next_count, 0, 20)
    && integer(value.blocker_count, 0, 20)
    && value.done_count + value.next_count + value.blocker_count > 0
}

/** Refuse the complete event, including its SSE cursor, without echoing any input. */
export function decodeWorkspaceChange(data: string, lastEventId: string): CheckpointCommittedEvent | null {
  try {
    const parsed: unknown = JSON.parse(data)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    const value = parsed as Record<string, unknown>
    if (Object.keys(value).length !== fields.length || !fields.every((field) => Object.hasOwn(value, field))) return null
    if (!integer(value.event_id, 1, Number.MAX_SAFE_INTEGER) || String(value.event_id) !== lastEventId) return null
    if (value.kind !== 'agent.checkpoint.committed' || value.origin !== 'agent-cli-v1' || value.replayed !== false) return null
    if (typeof value.first_for_task !== 'boolean' || !identifiers(value) || !counts(value)) return null
    return value as unknown as CheckpointCommittedEvent
  } catch {
    return null
  }
}

/**
 * The attributed transition notice. It has eleven fields like the committed
 * variant but a DIFFERENT exact schema, and it never carries reason,
 * explanation, a raw idempotency key or any other prose.
 */
export interface CheckpointTransitionEvent {
  event_id: number
  kind: 'agent.checkpoint.superseded' | 'agent.checkpoint.restored'
  workspace_uid: string
  task_id: string
  date: string
  checkpoint_id: string
  ordinal: number
  entry_digest: string
  state: 'superseded' | 'active'
  transition_revision: number
  origin: 'agent-cli-v1'
}

const transitionFields = [
  'event_id', 'kind', 'workspace_uid', 'task_id', 'date', 'checkpoint_id',
  'ordinal', 'entry_digest', 'state', 'transition_revision', 'origin',
] as const

const MAX_SAFE = 9007199254740991

/** Dedupe identity: revisions 1 and 3 for one checkpoint stay distinct. */
export function transitionNoticeKey(event: CheckpointTransitionEvent): string {
  return `${event.workspace_uid}:${event.checkpoint_id}:${event.transition_revision}`
}

/**
 * Refuse the complete transition notice without echoing any input. This is a
 * separate strict branch: it never accepts a committed notice and the committed
 * decoder never accepts one of these.
 */
/** Exactly the eleven transition fields, its SSE cursor and the frozen origin. */
function transitionEnvelope(value: Record<string, unknown>, lastEventId: string): boolean {
  if (Object.keys(value).length !== transitionFields.length) return false
  if (!transitionFields.every((field) => Object.hasOwn(value, field))) return false
  if (!integer(value.event_id, 1, MAX_SAFE) || String(value.event_id) !== lastEventId) return false
  return value.origin === 'agent-cli-v1'
}

/** State, kind and revision must agree; odd revisions supersede, even restore. */
function transitionParity(value: Record<string, unknown>): boolean {
  if (value.state !== 'superseded' && value.state !== 'active') return false
  const expected = value.state === 'superseded'
    ? 'agent.checkpoint.superseded'
    : 'agent.checkpoint.restored'
  if (value.kind !== expected) return false
  if (!integer(value.transition_revision, 1, MAX_SAFE)) return false
  const parity = value.transition_revision % 2 === 1 ? 'superseded' : 'active'
  return value.state === parity
}

/** The physical row this notice points at, in its canonical domains. */
function transitionCoordinates(value: Record<string, unknown>): boolean {
  return integer(value.ordinal, 0, MAX_SAFE)
    && identifiers(value)
    && matches(value.entry_digest, /^sha256:[0-9a-f]{64}$/)
}

export function decodeCheckpointTransition(
  data: string,
  lastEventId: string,
): CheckpointTransitionEvent | null {
  try {
    const parsed: unknown = JSON.parse(data)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    const value = parsed as Record<string, unknown>
    if (!transitionEnvelope(value, lastEventId)) return null
    if (!transitionParity(value)) return null
    if (!transitionCoordinates(value)) return null
    return value as unknown as CheckpointTransitionEvent
  } catch {
    return null
  }
}
