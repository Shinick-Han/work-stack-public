import frozen from '../../../tests/fixtures/checkpoint_change_v1.json'
import { decodeCheckpointTransition, transitionNoticeKey, decodeWorkspaceChange, WORKSPACE_CHANGE_EVENT } from './workspaceChangeEvents'

const base = {
  event_id: 1, kind: 'agent.checkpoint.committed', workspace_uid: '123e4567-e89b-42d3-a456-426614174000',
  task_id: 'T-0001', date: '2026-09-03', checkpoint_id: `CP-${'a'.repeat(64)}`,
  done_count: 1, next_count: 0, blocker_count: 0, first_for_task: true, origin: 'agent-cli-v1', replayed: false,
}

test('decodes every frozen successful notice from its independent canonical bytes', () => {
  expect(WORKSPACE_CHANGE_EVENT).toBe('workstack.change.v1')
  const notices = frozen.notice_vectors.filter((vector) => 'canonical_notice' in vector)
  expect(notices.length).toBeGreaterThan(0)
  for (const vector of notices) {
    const expected = vector.expected as typeof base
    expect(decodeWorkspaceChange(vector.canonical_notice!.text, String(expected.event_id)), vector.id).toEqual(expected)
  }
})

test.each(Object.keys(base))('refuses missing required field %s', (field) => {
  const value: Record<string, unknown> = { ...base }
  delete value[field]
  expect(decodeWorkspaceChange(JSON.stringify(value), '1')).toBeNull()
})

test.each([
  ['kind', 'agent.checkpoint.superseded'], ['origin', 'browser'], ['origin', null],
  ['replayed', true], ['replayed', 0], ['first_for_task', 1], ['first_for_task', 'false'],
  ['event_id', 0], ['event_id', -1], ['event_id', true], ['event_id', 1.5], ['event_id', Number.MAX_SAFE_INTEGER + 1],
  ['done_count', true], ['done_count', -1], ['next_count', 21], ['blocker_count', 0.5],
  ['workspace_uid', '123E4567-E89B-42D3-A456-426614174000'], ['workspace_uid', '00000000-0000-0000-0000-000000000000'],
  ['workspace_uid', '123e4567-e89b-42d3-7456-426614174000'], ['workspace_uid', `${base.workspace_uid}\n`],
  ['task_id', 'T-123'], ['task_id', 't-0001'], ['task_id', 'T-0001\n'], ['task_id', 'T-１２３４'],
  ['checkpoint_id', `CP-${'A'.repeat(64)}`], ['checkpoint_id', `${base.checkpoint_id}\n`],
  ['date', '2026-02-30'], ['date', '1900-02-29'], ['date', '0000-01-01'], ['date', '2026-9-03'],
  ['date', '2026-09-03\n'], ['date', '２０２６-09-03'], ['date', '2026-13-01'],
])('refuses invalid %s value %s', (field, value) => {
  expect(decodeWorkspaceChange(JSON.stringify({ ...base, [field as string]: value }), '1')).toBeNull()
})

test.each(['', '01', '2', '1\n', '+1', '1.0', '1e0'])('requires the canonical matching SSE id %s', (cursor) => {
  expect(decodeWorkspaceChange(JSON.stringify(base), cursor)).toBeNull()
})

test.each(['{', 'null', '[]', '"canary"', 'true'])('refuses non-record JSON %s without throwing', (value) => {
  expect(decodeWorkspaceChange(value, '1')).toBeNull()
})

test('refuses extras and zero total while accepting calendar/UUID boundaries without a clock', () => {
  expect(decodeWorkspaceChange(JSON.stringify({ ...base, raw_text: 'CANARY-SECRET' }), '1')).toBeNull()
  expect(decodeWorkspaceChange(JSON.stringify({ ...base, done_count: 0 }), '1')).toBeNull()
  for (const date of ['0001-01-01', '2000-02-29', '2024-02-29', '9999-12-31']) {
    const value = { ...base, date, workspace_uid: '123e4567-e89b-72d3-a456-426614174000', event_id: Number.MAX_SAFE_INTEGER }
    expect(decodeWorkspaceChange(JSON.stringify(value), String(value.event_id))).toEqual(value)
  }
})


const TRANSITION_WORKSPACE = 'f67e2aad-9ed9-4fc7-b1ae-63b240269855'
const TRANSITION_CP = `CP-${'a'.repeat(64)}`
const TRANSITION_DIGEST = `sha256:${'b'.repeat(64)}`

function transitionNotice(overrides: Record<string, unknown> = {}) {
  return {
    event_id: 5,
    kind: 'agent.checkpoint.superseded',
    workspace_uid: TRANSITION_WORKSPACE,
    task_id: 'T-0001',
    date: '2026-09-03',
    checkpoint_id: TRANSITION_CP,
    ordinal: 0,
    entry_digest: TRANSITION_DIGEST,
    state: 'superseded',
    transition_revision: 1,
    origin: 'agent-cli-v1',
    ...overrides,
  }
}

describe('transition notice decoder', () => {
  test('accepts the exact eleven-field attributed notice', () => {
    const decoded = decodeCheckpointTransition(JSON.stringify(transitionNotice()), '5')
    expect(decoded?.transition_revision).toBe(1)
    expect(decoded?.kind).toBe('agent.checkpoint.superseded')
    // SSE carries no reason or prose at all.
    expect(Object.keys(decoded ?? {})).not.toContain('reason')
  })

  test('accepts a restored notice on an even revision', () => {
    const decoded = decodeCheckpointTransition(
      JSON.stringify(transitionNotice({
        kind: 'agent.checkpoint.restored', state: 'active', transition_revision: 2,
      })),
      '5',
    )
    expect(decoded?.state).toBe('active')
  })

  test('refuses a reason field, a mismatched cursor, bad parity and a wrong kind', () => {
    const withReason = { ...transitionNotice(), reason: { code: 'incorrect', explanation: 'x' } }
    expect(decodeCheckpointTransition(JSON.stringify(withReason), '5')).toBeNull()
    expect(decodeCheckpointTransition(JSON.stringify(transitionNotice()), '6')).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ transition_revision: 2 })), '5',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ kind: 'agent.checkpoint.restored' })), '5',
    )).toBeNull()
  })

  test('the two variants never satisfy each other', () => {
    const notice = JSON.stringify(transitionNotice())
    expect(decodeWorkspaceChange(notice, '5')).toBeNull()
    const committed = JSON.stringify({
      event_id: 5,
      kind: 'agent.checkpoint.committed',
      workspace_uid: TRANSITION_WORKSPACE,
      task_id: 'T-0001',
      date: '2026-09-03',
      checkpoint_id: TRANSITION_CP,
      done_count: 1,
      next_count: 0,
      blocker_count: 0,
      first_for_task: true,
      origin: 'agent-cli-v1',
      replayed: false,
    })
    expect(decodeCheckpointTransition(committed, '5')).toBeNull()
    expect(decodeWorkspaceChange(committed, '5')).not.toBeNull()
  })

  test('dedupe identity keeps revisions 1 and 3 distinct', () => {
    const first = decodeCheckpointTransition(JSON.stringify(transitionNotice()), '5')!
    const third = decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ event_id: 7, transition_revision: 3 })), '7',
    )!
    expect(transitionNoticeKey(first)).not.toBe(transitionNoticeKey(third))
    expect(transitionNoticeKey(first)).toBe(`${TRANSITION_WORKSPACE}:${TRANSITION_CP}:1`)
  })
})

describe('transition decoder after the helper split', () => {
  /**
   * The decision moved into three helpers, so each of them must still be
   * reached. Every case below is otherwise a valid notice and fails in exactly
   * one helper.
   */
  test('the envelope still refuses a foreign origin and a missing field', () => {
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ origin: 'browser' })), '5',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ origin: null })), '5',
    )).toBeNull()

    const incomplete: Record<string, unknown> = { ...transitionNotice() }
    delete incomplete.entry_digest
    expect(decodeCheckpointTransition(JSON.stringify(incomplete), '5')).toBeNull()
  })

  test('the envelope still binds the cursor and refuses unsafe event ids', () => {
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ event_id: 0 })), '0',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ event_id: 9007199254740992 })), '9007199254740992',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ event_id: 5.5 })), '5.5',
    )).toBeNull()
  })

  test('parity still refuses an unknown state and a non-integer revision', () => {
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ state: 'archived', kind: 'agent.checkpoint.superseded' })), '5',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ transition_revision: 0 })), '5',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ transition_revision: '1' })), '5',
    )).toBeNull()
  })

  test('coordinates are still validated even when envelope and parity agree', () => {
    // Each of these differs from a healthy notice only in one coordinate.
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ ordinal: -1 })), '5',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ ordinal: 1.5 })), '5',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ entry_digest: `sha256:${'g'.repeat(64)}` })), '5',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ checkpoint_id: `CP-${'a'.repeat(63)}` })), '5',
    )).toBeNull()
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ date: '2026-02-30' })), '5',
    )).toBeNull()
    // A trailing newline must not be accepted by the anchored patterns.
    expect(decodeCheckpointTransition(
      JSON.stringify(transitionNotice({ task_id: 'T-0001\n' })), '5',
    )).toBeNull()
  })

  test('an unreadable payload is still a content-free refusal, not a throw', () => {
    expect(decodeCheckpointTransition('{not json', '5')).toBeNull()
    expect(decodeCheckpointTransition('[]', '5')).toBeNull()
    expect(decodeCheckpointTransition('null', '5')).toBeNull()
  })

  test('healthy notices at both parities still decode unchanged', () => {
    const odd = decodeCheckpointTransition(JSON.stringify(transitionNotice()), '5')
    expect(odd?.state).toBe('superseded')
    expect(odd?.ordinal).toBe(0)
    const even = decodeCheckpointTransition(
      JSON.stringify(transitionNotice({
        kind: 'agent.checkpoint.restored', state: 'active', transition_revision: 4,
      })),
      '5',
    )
    expect(even?.state).toBe('active')
    expect(even?.transition_revision).toBe(4)
  })
})
