/**
 * Readable projection of the opaque checkpoint payload.
 *
 * The audit carries the ORIGINAL worklog row verbatim, so this module is the
 * one place that interprets it. It is pure and total: a malformed, legacy or
 * hostile shape resolves to a summary instead of throwing, and a payload
 * nothing could be read from is reported as an explicit fallback rather than
 * being silently dropped. Nothing here is trusted content — every recovered
 * string is carried as TEXT for the view to escape, never as markup.
 *
 * Recognition is partial by design, so it is also LOSSLESS. A payload can be
 * half understood — a known Task with an unknown sibling field, or a Done list
 * holding one string and one number — and everything the readable summary could
 * not render is reported in `extras` instead of being quietly discarded. An
 * audit that hides part of the record is worse than one that looks untidy.
 *
 * The one asymmetry is deliberate. A WHOLE field that is missing, null or blank
 * is absence and says nothing; a SLOT inside a list is a position its author
 * wrote, so `done: null` is silence while `done: ['kept', null]` reports
 * `done[1]: null`. The bullets plus the leftovers rebuild the original list.
 */

export type CheckpointFactField = 'done' | 'next' | 'blockers'

/** Presentation order: what happened, what is next, what is in the way. */
export const CHECKPOINT_FACT_FIELDS: readonly CheckpointFactField[] = ['done', 'next', 'blockers']

export const CHECKPOINT_FACT_LABELS: Record<CheckpointFactField, string> = {
  done: 'Done',
  next: 'Next',
  blockers: 'Blockers',
}

/** Fields the readable summary knows how to render. Everything else is extra. */
const KNOWN_FIELDS: ReadonlySet<string> = new Set([
  'task_id',
  'task',
  ...CHECKPOINT_FACT_FIELDS,
])

/** One value the summary above did not render, kept discoverable. */
export interface CheckpointExtra {
  /** Where the value came from: a field name, or `field[index]` for a list. */
  label: string
  /** The value as display text. Rendered as text; it is never markup. */
  value: string
}

export interface CheckpointEntrySummary {
  taskId: string | null
  taskTitle: string | null
  done: readonly string[]
  next: readonly string[]
  blockers: readonly string[]
  /** True once any human-facing field was recovered from the payload. */
  readable: boolean
  /** Last-resort text, present exactly when `readable` is false. */
  fallback: string | null
  /**
   * Unrecognized fields and every list slot that did not become a bullet, in
   * the payload's own key order and at the original index. When nothing was
   * recognized at all this holds the WHOLE payload, field by field, so the body
   * never has to fall back to a JSON dump.
   */
  extras: readonly CheckpointExtra[]
}

/** A payload that carried nothing at all is a fact, not an error. */
export const NO_ENTRY_CONTENT = 'No entry content available'

/** A single value we can neither read nor even serialize for inspection. */
export const UNDISPLAYABLE_ENTRY = 'Entry content is not displayable'

/**
 * A payload that carried data but none this view can narrate. Its fields are
 * all reported as extras, so the body says what happened instead of printing
 * the serialized record where the summary belongs.
 */
export const NO_READABLE_SUMMARY = 'No readable summary for this entry'

export const CHECKPOINT_STATE_LABELS = {
  active: 'Active',
  superseded: 'Superseded',
} as const

/** The audit state said plainly; an unknown state is shown, never guessed. */
export function checkpointStateLabel(state: string): string {
  return state === 'superseded' || state === 'active'
    ? CHECKPOINT_STATE_LABELS[state]
    : state
}

function textValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null
}

/**
 * Tolerates the legacy single-string form and drops blank items, so a row
 * written before the list shape existed still reads as one bullet. Anything it
 * does not return is reported by `collectExtras`, never lost.
 */
function textList(value: unknown): string[] {
  const single = textValue(value)
  if (single !== null) return [single]
  if (!Array.isArray(value)) return []
  const items: string[] = []
  for (const item of value) {
    const text = textValue(item)
    if (text !== null) items.push(text)
  }
  return items
}

function safeJson(value: unknown): string | null {
  try {
    const text = JSON.stringify(value)
    return text === undefined ? null : text
  } catch {
    // Cyclic or otherwise unserializable: there is nothing safe left to show.
    return null
  }
}

/** Total: every value becomes some readable text, and none of it is markup. */
function displayText(value: unknown): string {
  // A blank string is quoted so the reader can see that the slot held
  // whitespace; anything else readable is its own best rendering.
  if (typeof value === 'string') {
    return value.trim() === '' ? safeJson(value) ?? '""' : value
  }
  const json = safeJson(value)
  if (json !== null) return json
  if (typeof value === 'object') return UNDISPLAYABLE_ENTRY
  try {
    // BigInt and other JSON-hostile scalars still have a faithful text form.
    return String(value)
  } catch {
    return UNDISPLAYABLE_ENTRY
  }
}

/**
 * Absent is not the same as dropped. A WHOLE field that is missing, null or
 * blank never happened, so it has nothing to report — but this never applies
 * inside a list, where every slot is a position the author actually wrote.
 */
function carriesNothing(value: unknown): boolean {
  return value === null
    || value === undefined
    || (typeof value === 'string' && value.trim() === '')
}

/**
 * Whatever `textList` could not turn into a bullet, named by its position.
 *
 * Every SLOT the list held is reported, including a null, an empty string or a
 * hole. The index is part of the record — a reader has to be able to rebuild
 * the original list from the bullets plus these leftovers — so a slot is never
 * quietly closed up, and the loop is indexed rather than `forEach` so a sparse
 * hole is reported instead of skipped.
 */
function factFieldExtras(field: string, value: unknown): CheckpointExtra[] {
  // A legacy single string became the one bullet; nothing is left over.
  if (carriesNothing(value) || typeof value === 'string') return []
  if (!Array.isArray(value)) return [{ label: field, value: displayText(value) }]
  const extras: CheckpointExtra[] = []
  for (let index = 0; index < value.length; index += 1) {
    const item: unknown = value[index]
    if (textValue(item) !== null) continue
    extras.push({ label: `${field}[${index}]`, value: displayText(item) })
  }
  return extras
}

/**
 * Everything the readable summary leaves behind, walked in the payload's own
 * key order so the record still reads the way its author wrote it.
 */
function collectExtras(source: Record<string, unknown>): CheckpointExtra[] {
  const extras: CheckpointExtra[] = []
  for (const [key, value] of Object.entries(source)) {
    if (!KNOWN_FIELDS.has(key)) {
      // An unknown key is itself the finding, so it is reported even when its
      // value is empty.
      extras.push({ label: key, value: displayText(value) })
    } else if (key === 'task_id' || key === 'task') {
      if (!carriesNothing(value) && textValue(value) === null) {
        extras.push({ label: key, value: displayText(value) })
      }
    } else {
      extras.push(...factFieldExtras(key, value))
    }
  }
  return extras
}

/** A bare list has no field names, so its own indices are the labels. */
function listExtras(value: readonly unknown[]): CheckpointExtra[] {
  const extras: CheckpointExtra[] = []
  for (let index = 0; index < value.length; index += 1) {
    extras.push({ label: `[${index}]`, value: displayText(value[index]) })
  }
  return extras
}

function unreadable(fallback: string, extras: CheckpointExtra[] = []): CheckpointEntrySummary {
  return {
    taskId: null,
    taskTitle: null,
    done: [],
    next: [],
    blockers: [],
    readable: false,
    fallback,
    extras,
  }
}

/** Nothing was narratable: say so, and hand every field to the extras panel. */
function onlyExtras(extras: CheckpointExtra[]): CheckpointEntrySummary {
  return unreadable(extras.length === 0 ? NO_ENTRY_CONTENT : NO_READABLE_SUMMARY, extras)
}

export function summarizeCheckpointEntry(entry: unknown): CheckpointEntrySummary {
  if (entry === null || entry === undefined) return unreadable(NO_ENTRY_CONTENT)
  // A legacy row was free text; that text is already the readable summary.
  if (typeof entry === 'string') return unreadable(textValue(entry) ?? NO_ENTRY_CONTENT)
  if (Array.isArray(entry)) return onlyExtras(listExtras(entry))
  // A bare scalar is already its own readable text.
  if (typeof entry !== 'object') return unreadable(displayText(entry))

  const source = entry as Record<string, unknown>
  const summary: CheckpointEntrySummary = {
    taskId: textValue(source.task_id),
    taskTitle: textValue(source.task),
    done: textList(source.done),
    next: textList(source.next),
    blockers: textList(source.blockers),
    readable: true,
    fallback: null,
    extras: collectExtras(source),
  }
  const recovered = summary.taskId !== null
    || summary.taskTitle !== null
    || summary.done.length > 0
    || summary.next.length > 0
    || summary.blockers.length > 0
  if (recovered) return summary
  // No known field survived, so the WHOLE payload is the leftover. It is
  // reported field by field rather than dumped as JSON where the summary
  // belongs: an opaque row is still an inspectable one.
  return onlyExtras(summary.extras as CheckpointExtra[])
}
