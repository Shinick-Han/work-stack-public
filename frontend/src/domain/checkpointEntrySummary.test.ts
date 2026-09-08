import { describe, expect, test } from 'vitest'

import {
  NO_ENTRY_CONTENT,
  NO_READABLE_SUMMARY,
  UNDISPLAYABLE_ENTRY,
  checkpointStateLabel,
  summarizeCheckpointEntry,
} from './checkpointEntrySummary'

/**
 * The audit hands this module an OPAQUE payload. Every case below is a shape a
 * real or legacy worklog row can actually take, and none of them may throw:
 * an unreadable payload has to degrade to a stated fallback, never to a crash
 * and never to a silently empty summary that implies nothing was recorded.
 */

describe('a well-formed worklog row', () => {
  test('reads task identity and all three fact lists', () => {
    const summary = summarizeCheckpointEntry({
      task_id: 'T-0001',
      task: 'Define release quality gate',
      done: ['Drafted release gate sections'],
      next: ['Validate with engineering'],
      blockers: ['Waiting on legal'],
    })
    expect(summary).toEqual({
      taskId: 'T-0001',
      taskTitle: 'Define release quality gate',
      done: ['Drafted release gate sections'],
      next: ['Validate with engineering'],
      blockers: ['Waiting on legal'],
      readable: true,
      fallback: null,
      // Everything was understood, so there is nothing left to report.
      extras: [],
    })
  })

  test('an empty list stays empty and still counts as readable', () => {
    const summary = summarizeCheckpointEntry({ task_id: 'T-0002', done: [], next: [], blockers: [] })
    expect(summary.readable).toBe(true)
    expect(summary.blockers).toEqual([])
    expect(summary.fallback).toBeNull()
  })

  test('a single fact list alone is enough to be readable', () => {
    const summary = summarizeCheckpointEntry({ done: ['shipped'] })
    expect(summary.readable).toBe(true)
    expect(summary.done).toEqual(['shipped'])
    expect(summary.taskId).toBeNull()
    expect(summary.taskTitle).toBeNull()
  })
})

describe('malformed and legacy payloads degrade instead of throwing', () => {
  test('a null payload states that there is no content', () => {
    expect(summarizeCheckpointEntry(null)).toMatchObject({
      readable: false,
      fallback: NO_ENTRY_CONTENT,
    })
    expect(summarizeCheckpointEntry(undefined).fallback).toBe(NO_ENTRY_CONTENT)
  })

  test('a legacy free-text payload is itself the summary, verbatim', () => {
    const summary = summarizeCheckpointEntry('  shipped the gate  ')
    expect(summary.readable).toBe(false)
    // Whitespace survives: normalization is the server's job, not the view's.
    expect(summary.fallback).toBe('  shipped the gate  ')
  })

  test('a blank string payload reports no content rather than an empty line', () => {
    expect(summarizeCheckpointEntry('   ').fallback).toBe(NO_ENTRY_CONTENT)
  })

  test('a cyclic payload names the field it could not serialize', () => {
    const cyclic: Record<string, unknown> = {}
    cyclic.self = cyclic
    const summary = summarizeCheckpointEntry(cyclic)
    expect(summary.readable).toBe(false)
    expect(summary.fallback).toBe(NO_READABLE_SUMMARY)
    expect(summary.extras).toEqual([{ label: 'self', value: UNDISPLAYABLE_ENTRY }])
  })

  test('an empty object has nothing to report at all', () => {
    expect(summarizeCheckpointEntry({})).toMatchObject({
      readable: false,
      fallback: NO_ENTRY_CONTENT,
      extras: [],
    })
  })

  test('an unrecognized object is reported field by field, not as a JSON body', () => {
    const summary = summarizeCheckpointEntry({ shape: 'unknown', count: 2 })
    expect(summary.readable).toBe(false)
    // The body states what happened; the payload lives in the extras panel.
    expect(summary.fallback).toBe(NO_READABLE_SUMMARY)
    expect(summary.extras).toEqual([
      { label: 'shape', value: 'unknown' },
      { label: 'count', value: '2' },
    ])
  })

  test('a scalar or array payload is not mistaken for a row', () => {
    expect(summarizeCheckpointEntry(42).fallback).toBe('42')
    const list = summarizeCheckpointEntry(['done', 7])
    expect(list.fallback).toBe(NO_READABLE_SUMMARY)
    expect(list.extras).toEqual([
      { label: '[0]', value: 'done' },
      { label: '[1]', value: '7' },
    ])
  })

  test('a fact list written as one legacy string becomes one item', () => {
    const summary = summarizeCheckpointEntry({ done: 'shipped', next: null, blockers: 7 })
    expect(summary.done).toEqual(['shipped'])
    expect(summary.next).toEqual([])
    expect(summary.blockers).toEqual([])
  })

  test('every list slot that did not become a bullet is reported at its index', () => {
    const summary = summarizeCheckpointEntry({ done: ['kept', '', '   ', 3, null, 'also kept'] })
    expect(summary.done).toEqual(['kept', 'also kept'])
    // Inside a list a slot is a position its author wrote, so none of these is
    // absence: the bullets plus these leftovers rebuild the original six.
    expect(summary.extras).toEqual([
      { label: 'done[1]', value: '""' },
      { label: 'done[2]', value: '"   "' },
      { label: 'done[3]', value: '3' },
      { label: 'done[4]', value: 'null' },
    ])
  })

  test('a blank task identity is treated as absent, not as an empty heading', () => {
    const summary = summarizeCheckpointEntry({ task_id: '   ', task: '', done: ['x'] })
    expect(summary.taskId).toBeNull()
    expect(summary.taskTitle).toBeNull()
  })
})

describe('state labels', () => {
  test('the two known states read as words', () => {
    expect(checkpointStateLabel('active')).toBe('Active')
    expect(checkpointStateLabel('superseded')).toBe('Superseded')
  })

  test('an unknown state is shown as received rather than guessed', () => {
    expect(checkpointStateLabel('quarantined')).toBe('quarantined')
  })
})

/**
 * The reviewer finding: recognition is PARTIAL, so a payload can be half
 * understood. Everything the readable summary cannot render has to stay
 * discoverable — an audit that silently drops half a record is worse than one
 * that looks untidy.
 */
describe('partially recognized payloads keep every unrendered value', () => {
  test('the reported case: a dropped list number and an unknown field survive', () => {
    const summary = summarizeCheckpointEntry({
      task_id: 'T-1',
      done: ['kept', 42],
      legacy_note: 'critical',
    })

    // The human summary is unchanged: the readable half still leads.
    expect(summary.taskId).toBe('T-1')
    expect(summary.done).toEqual(['kept'])
    expect(summary.readable).toBe(true)
    expect(summary.fallback).toBeNull()

    // The other half is reported rather than discarded.
    expect(summary.extras).toEqual([
      { label: 'done[1]', value: '42' },
      { label: 'legacy_note', value: 'critical' },
    ])
  })

  test('extras follow the payload key order the author wrote', () => {
    const summary = summarizeCheckpointEntry({
      legacy_note: 'first',
      task: 'Ship it',
      reviewer: 'second',
      next: [7],
    })
    expect(summary.extras.map((extra) => extra.label))
      .toEqual(['legacy_note', 'reviewer', 'next[0]'])
  })

  test('a whole fact field of the wrong type is reported under its own name', () => {
    const summary = summarizeCheckpointEntry({ done: ['shipped'], blockers: 7 })
    expect(summary.blockers).toEqual([])
    expect(summary.extras).toEqual([{ label: 'blockers', value: '7' }])
  })

  test('a non-string task identity is reported instead of vanishing', () => {
    const summary = summarizeCheckpointEntry({ task_id: 42, task: 'Ship it' })
    expect(summary.taskId).toBeNull()
    expect(summary.taskTitle).toBe('Ship it')
    expect(summary.extras).toEqual([{ label: 'task_id', value: '42' }])
  })

  test('an unknown key is reported even when its value is empty', () => {
    const summary = summarizeCheckpointEntry({ done: ['shipped'], reviewer: null })
    expect(summary.extras).toEqual([{ label: 'reviewer', value: 'null' }])
  })

  test('nested and exotic values still resolve to readable text', () => {
    const cyclic: Record<string, unknown> = {}
    cyclic.self = cyclic
    const summary = summarizeCheckpointEntry({
      done: ['shipped'],
      nested: { owner: 'ana', tags: ['a', 1] },
      flagged: true,
      huge: 10n,
      broken: cyclic,
    })
    expect(summary.extras).toEqual([
      { label: 'nested', value: '{"owner":"ana","tags":["a",1]}' },
      { label: 'flagged', value: 'true' },
      { label: 'huge', value: '10' },
      { label: 'broken', value: UNDISPLAYABLE_ENTRY },
    ])
  })

  test('an unreadable payload still surfaces every field it carried', () => {
    const summary = summarizeCheckpointEntry({ shape: 'unknown', other: 1 })
    expect(summary.readable).toBe(false)
    expect(summary.fallback).toBe(NO_READABLE_SUMMARY)
    expect(summary.extras).toEqual([
      { label: 'shape', value: 'unknown' },
      { label: 'other', value: '1' },
    ])
  })

  test('extras carry markup as inert text, never as a parsed value', () => {
    const summary = summarizeCheckpointEntry({
      done: ['shipped'],
      legacy_note: '<img src=x onerror="alert(1)">',
    })
    // Verbatim characters: escaping is the view's job and it never opts out.
    expect(summary.extras).toEqual([
      { label: 'legacy_note', value: '<img src=x onerror="alert(1)">' },
    ])
  })
})

/**
 * The re-review finding: a null INSIDE a list was being dropped like an absent
 * field. A field and a slot are different facts — `done: null` never happened,
 * `done: ['kept', null]` happened and holds a null at index 1 — and only the
 * first of those may be silent.
 */
describe('null list slots survive, whole null fields stay absent', () => {
  test('the reported case: done[1] null is preserved with its index', () => {
    const summary = summarizeCheckpointEntry({ task_id: 'T-1', done: ['kept', null] })
    expect(summary.done).toEqual(['kept'])
    expect(summary.extras).toEqual([{ label: 'done[1]', value: 'null' }])
  })

  test('a whole null field remains semantic absence and reports nothing', () => {
    const summary = summarizeCheckpointEntry({ task_id: 'T-1', done: null, next: undefined })
    expect(summary.done).toEqual([])
    expect(summary.next).toEqual([])
    expect(summary.extras).toEqual([])
  })

  test('a mixed payload keeps bullets, slots and unknown fields all distinct', () => {
    const summary = summarizeCheckpointEntry({
      task_id: 'T-1',
      task: 'Ship the gate',
      done: ['kept', null, 42, '', 'also kept'],
      next: null,
      blockers: [null],
      legacy_note: 'critical',
    })

    // The readable half is untouched and still leads the card.
    expect(summary.readable).toBe(true)
    expect(summary.taskId).toBe('T-1')
    expect(summary.taskTitle).toBe('Ship the gate')
    expect(summary.done).toEqual(['kept', 'also kept'])
    expect(summary.blockers).toEqual([])

    // `next: null` is silence; every list slot and the unknown field are not.
    expect(summary.extras).toEqual([
      { label: 'done[1]', value: 'null' },
      { label: 'done[2]', value: '42' },
      { label: 'done[3]', value: '""' },
      { label: 'blockers[0]', value: 'null' },
      { label: 'legacy_note', value: 'critical' },
    ])
  })

  test('a list of nothing but nulls is still a list of two recorded slots', () => {
    const summary = summarizeCheckpointEntry({ task: 'Ship it', blockers: [null, null] })
    expect(summary.blockers).toEqual([])
    expect(summary.extras).toEqual([
      { label: 'blockers[0]', value: 'null' },
      { label: 'blockers[1]', value: 'null' },
    ])
  })

  test('a sparse hole is reported rather than skipped, so length survives', () => {
    const sparse = ['kept', , 'also kept'] as unknown[]
    const summary = summarizeCheckpointEntry({ done: sparse })
    expect(summary.done).toEqual(['kept', 'also kept'])
    expect(summary.extras).toEqual([{ label: 'done[1]', value: 'undefined' }])
  })

  test('a bare list payload keeps its null slots too', () => {
    const summary = summarizeCheckpointEntry(['done', null])
    expect(summary.extras).toEqual([
      { label: '[0]', value: 'done' },
      { label: '[1]', value: 'null' },
    ])
  })
})
