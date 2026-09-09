import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from 'vitest'

import type { CheckpointAudit } from '../../domain/types'
import { selectTaskResumeFacts, type TaskResumeFacts } from './taskResumeFacts'

/**
 * The GUI selector is the oracle for `workstack.checkpoint-facts.v1`.
 *
 * `workstack/checkpoint_facts.py` reimplements this module's selection, its
 * summary semantics and its `version` identity for the CLI. Two implementations
 * of one behaviour drift silently unless something compares them, so both read
 * the same committed cases and must produce the same answers. The expected
 * documents in that file are written by hand from the frozen contract; neither
 * implementation generates them.
 *
 * Nothing here changes production selection. The projection below only renames
 * the oracle's own outputs into the wire spelling, so a disagreement is always
 * a disagreement about behaviour rather than about field names.
 */

const FIXTURES = join(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../contracts/checkpoint-facts-v1/cases.json',
)

const CONTRACT = 'workstack.checkpoint-facts.v1'

/** The original GUI facts statuses. The progress adapter's renames are not used. */
const SUCCESS_STATUSES = ['empty', 'unreadable', 'partial', 'ready']

/** U+0085. Written as a code point so no editor can normalize it away. */
const NEXT_LINE = String.fromCharCode(0x85)

type SharedCase = {
  id: string
  purpose: string
  workspace_uid: string
  task_id: string
  audit: unknown
  expected: Record<string, unknown>
}

const payload = JSON.parse(readFileSync(FIXTURES, 'utf8')) as {
  contract: string
  cases: SharedCase[]
}

/**
 * The fixed reason order, derived from the oracle's own outputs rather than
 * from any Python decision: values it could not present, a payload disputing
 * the locator it was filed under, and a summary nothing could be read from.
 */
function reasonsOf(facts: TaskResumeFacts): string[] {
  const codes: string[] = []
  if (facts.extras.length > 0) codes.push('unpresented_values')
  const provenance = facts.provenance
  const disputed = provenance !== null
    && provenance.binding === 'locator'
    && provenance.recordedTaskId !== null
    && provenance.recordedTaskId !== facts.taskId
  if (disputed) codes.push('recorded_task_mismatch')
  if (facts.status === 'unreadable') codes.push('no_readable_summary')
  return codes
}

/** The wire document: the oracle's facts, renamed and nothing added. */
function projected(facts: TaskResumeFacts): Record<string, unknown> {
  const source = facts.provenance
  return {
    active_record_count: facts.activeRecordCount,
    blockers: [...facts.blockers],
    contract: CONTRACT,
    done: [...facts.done],
    next: [...facts.next],
    provenance: source === null ? null : {
      binding: source.binding,
      checkpoint_id: source.checkpointId,
      date: source.date,
      entry_digest: source.entryDigest,
      ordinal: source.ordinal,
      origin: source.origin,
      recorded_task_id: source.recordedTaskId,
      recorded_task_title: source.recordedTaskTitle,
      revision: source.revision,
    },
    reasons: reasonsOf(facts),
    status: facts.status,
    superseded_record_count: facts.supersededRecordCount,
    task_id: facts.taskId,
    version: facts.version,
    workspace_uid: facts.workspaceUid,
  }
}

function select(item: SharedCase): TaskResumeFacts {
  return selectTaskResumeFacts(item.workspace_uid, item.task_id, {
    audit: item.audit as CheckpointAudit,
    isPending: false,
    error: null,
  })
}

test('the shared checkpoint-facts cases exist as committed JSON', () => {
  expect(payload.contract).toBe(CONTRACT)
  expect(readdirSync(dirname(FIXTURES))).toContain('cases.json')
  expect(payload.cases.map((item) => item.id)).toEqual([
    'empty-no-record-for-task',
    'latest-day-wins',
    'same-day-ordinal-order',
    'superseded-latest-excluded-but-counted',
    'every-record-superseded-is-empty',
    'unreadable-latest-never-falls-back',
    'legacy-free-text-entry-is-unreadable',
    'opaque-payload-counts-without-presenting',
    'partial-mixed-slots-and-unknown-field',
    'recorded-task-mismatch-is-partial',
    'legacy-entry-payload-binding-declared',
    'legacy-row-naming-another-task-is-not-borrowed',
    'foreign-workspace-and-task-ignored',
    'checkpoint-only-revision-changes-identity',
    'fence-and-utf8-canaries-preserved',
  ])
})

test('the GUI selector produces the expected facts for every shared case', () => {
  for (const item of payload.cases) {
    expect(projected(select(item)), item.id).toEqual(item.expected)
  }
})

test('a read snapshot only ever reaches the four original GUI statuses', () => {
  for (const item of payload.cases) {
    // loading and error are lifetime states of a QUERY. A pure projection over
    // a snapshot the caller already read can never be in either, and the
    // renamed none/unavailable spellings of the progress adapter are not used.
    expect(SUCCESS_STATUSES, item.id).toContain(select(item).status)
  }
})

test('an unpresentable value is counted and never carried into the document', () => {
  const cases = payload.cases.filter(
    (item) => (item.expected.reasons as string[]).includes('unpresented_values'),
  )
  expect(cases.map((item) => item.id)).toEqual([
    'opaque-payload-counts-without-presenting',
    'partial-mixed-slots-and-unknown-field',
  ])
  for (const item of cases) {
    const facts = select(item)
    // The oracle keeps the leftovers discoverable in the drawer; the wire
    // document must not reproduce a single one of them.
    expect(facts.extras.length, item.id).toBeGreaterThan(0)
    const rendered = JSON.stringify(projected(facts))
    expect(rendered, item.id).not.toContain('unknown_shape')
    expect(rendered, item.id).not.toContain('reviewer')
    expect(rendered, item.id).not.toContain('unnamed')
  }
})

test('the unreadable latest is reported without an older record standing in', () => {
  const item = payload.cases.find(
    (entry) => entry.id === 'unreadable-latest-never-falls-back',
  )
  const facts = select(item as SharedCase)
  expect(facts.status).toBe('unreadable')
  expect(facts.next).toEqual([])
  // The older readable row is still counted, and still not selected.
  expect(facts.activeRecordCount).toBe(2)
  expect(facts.provenance?.date).toBe('2026-09-06')
})

test('blank-slot handling agrees on U+0085, which only JavaScript keeps', () => {
  const item = payload.cases.find(
    (entry) => entry.id === 'partial-mixed-slots-and-unknown-field',
  )
  const facts = select(item as SharedCase)
  // Python's str.strip() would drop this slot and report it as a leftover;
  // String.prototype.trim keeps it. The shared expectation is the oracle's.
  expect(facts.done).toEqual(['drafted', NEXT_LINE])
  expect(facts.next).toEqual(['review'])
})

test('untrusted text is preserved exactly, markup and typed URL included', () => {
  const item = payload.cases.find(
    (entry) => entry.id === 'fence-and-utf8-canaries-preserved',
  )
  const facts = select(item as SharedCase)
  expect(facts.status).toBe('ready')
  expect(facts.done).toEqual(['```\n# Not a heading', '- [ ] not a list'])
  expect(facts.next).toEqual(['https://leak.example/path typed by the user'])
  expect(facts.blockers).toEqual(['`````'])
  expect(facts.provenance?.recordedTaskTitle).toBe('``` 요약 ✅')
})
