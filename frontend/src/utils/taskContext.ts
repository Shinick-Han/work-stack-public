import type { ContextItem } from '../domain/types'

/**
 * Q4: neutral context helpers shared by the Task feature and the Workspace
 * Graph popover. The Task feature keeps re-exporting them, so its consumers and
 * tests are unchanged; the bodies below are the originals.
 */

const KNOWN_CONTEXT_KEYS = [
  'id',
  'ref',
  'connections',
  'date_precision',
  'kind',
  'type',
  'text',
  'created',
  'created_at',
  'source',
  'normalized',
  'provenance',
] as const

const SOURCE_ALREADY_SHOWN = new Set(['provider', 'web_url'])
const NORMALIZED_ALREADY_SHOWN = new Set(['action_items'])

export function externalContext(item: ContextItem) {
  return Boolean(item.source || item.normalized || item.kind === 'capture' || item.type === 'capture')
}

export function contextTitle(item: ContextItem) {
  return item.source?.display_title ?? item.normalized?.summary ?? item.text ?? 'Context item'
}

/**
 * Body copy for a context record. Omitted when it would reprint the heading, so
 * a plain-text note is shown once while a real source title still keeps its body.
 * When normalized.context equals the heading, a distinct item.text still renders.
 */
export function contextPlainBody(item: ContextItem): string | null {
  const title = contextTitle(item)
  const primary = item.normalized?.context ? item.normalized.context : null
  if (primary !== null && primary !== title) return primary
  const fallback = item.text ? item.text : null
  if (fallback !== null && fallback !== title) return fallback
  return null
}

export interface ContextUnknownField {
  label: string
  value: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function displayRecordedValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (value === null) return 'null'
  try {
    return JSON.stringify(value)
  } catch {
    return 'Unreadable value'
  }
}

function appendRecordedField(
  extras: ContextUnknownField[],
  label: string,
  value: unknown,
): void {
  if (value === undefined) return
  if (Array.isArray(value) && value.length === 0) return
  extras.push({ label, value: displayRecordedValue(value) })
}

function appendRecordedOwnKeys(
  extras: ContextUnknownField[],
  prefix: string,
  value: unknown,
  skip: (key: string, entry: unknown) => boolean,
): void {
  if (!isRecord(value)) return
  for (const [key, entry] of Object.entries(value)) {
    if (skip(key, entry)) continue
    appendRecordedField(extras, `${prefix}.${key}`, entry)
  }
}

/**
 * Recorded fields that the timeline does not already render as title, body,
 * time, pills, action items, or the source link. Top-level unknown keys stay
 * inspectable, and so do provenance, connections, unrendered source attributes,
 * normalized tags, and unknown nested own-keys. Visible title/body values are
 * not repeated. Missing keys are omitted; nothing is invented.
 */
export function contextUnknownFields(item: ContextItem): ContextUnknownField[] {
  const extras: ContextUnknownField[] = []
  const title = contextTitle(item)
  const body = contextPlainBody(item)

  for (const [key, value] of Object.entries(item)) {
    if ((KNOWN_CONTEXT_KEYS as readonly string[]).includes(key)) continue
    appendRecordedField(extras, key, value)
  }

  appendRecordedOwnKeys(extras, 'provenance', item.provenance, () => false)
  appendRecordedField(extras, 'connections', item.connections)
  appendRecordedOwnKeys(extras, 'source', item.source, (key, entry) => (
    SOURCE_ALREADY_SHOWN.has(key) || (key === 'display_title' && entry === title)
  ))
  appendRecordedOwnKeys(extras, 'normalized', item.normalized, (key, entry) => (
    NORMALIZED_ALREADY_SHOWN.has(key)
    || (key === 'summary' && entry === title)
    || (key === 'context' && (entry === title || entry === body))
  ))
  return extras
}

/**
 * R22: whether this card is the explicit Capture link of THIS Task, and the exact
 * Capture revision the reader is looking at.
 *
 * Every clause is a refusal the contract asks for by name:
 *
 * - only a `capture` card with shared identity — a note card, and a legacy card with no
 *   `ref`/`connections` at all, has no action;
 * - only a `capture-link` reason toward this exact Task id — a conversion-only card is
 *   an origin, not a reversible reference, and another Task's link is not this Task's;
 * - only a nonnegative safe-integer `revision` — with no displayed revision there is no
 *   CAS to send, so the action is not offered rather than sent blind.
 *
 * A card that carries BOTH reasons is removable: the explicit link goes and the
 * conversion keeps the card, which is exactly what the contract publishes.
 */
export interface CaptureLinkRemovalTarget {
  captureId: string
  revision: number
}

export function captureLinkRemovalTarget(
  item: ContextItem,
  taskId: string,
): CaptureLinkRemovalTarget | null {
  const ref = item.ref
  if (!taskId || !ref || ref.kind !== 'capture' || !ref.id) return null
  const connections = item.connections
  if (!Array.isArray(connections)) return null
  const linked = connections.some((connection) => (
    connection.target.kind === 'task'
    && connection.target.id === taskId
    && connection.reasons.includes('capture-link')
  ))
  if (!linked) return null
  const revision: unknown = item.revision
  if (typeof revision !== 'number' || !Number.isSafeInteger(revision) || revision < 0) return null
  return { captureId: ref.id, revision }
}
