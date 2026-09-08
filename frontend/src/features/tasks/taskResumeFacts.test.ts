import { describe, expect, test } from 'vitest'

import { NO_ENTRY_CONTENT, NO_READABLE_SUMMARY } from '../../domain/checkpointEntrySummary'
import type { CheckpointAudit, CheckpointAuditEntry } from '../../domain/types'
import { selectTaskResumeFacts, taskResumeAuditQueryKey } from './taskResumeFacts'

/**
 * Resume tells a returning reader what they said they would do next. Every case
 * below is a way that promise can be broken quietly: a superseded record read
 * as current, a stale blocker carried across a newer record that cleared it,
 * another Task's plan surviving a navigation, a broken latest entry papered
 * over with an older readable one, or a prepared brief that never notices a new
 * checkpoint because the Task's own revision did not move.
 */

const WORKSPACE = '123e4567-e89b-42d3-a456-426614174000'
const OTHER_WORKSPACE = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const TASK = 'T-0033'
const OTHER_TASK = 'T-0041'

const READ = { isPending: false, error: null }

function checkpointId(seed: string): string {
  return `CP-${seed.repeat(64).slice(0, 64)}`
}

function digest(seed: string): string {
  return `sha256:${seed.repeat(64).slice(0, 64)}`
}

interface EntryOptions {
  taskId?: string | null
  date?: string
  ordinal?: number
  state?: 'active' | 'superseded'
  revision?: number
  payload?: unknown
  workspaceUid?: string
  /** A row recorded before checkpoint identity existed. */
  legacy?: boolean
  origin?: 'agent-cli-v1' | null
  seed?: string
}

function entry(options: EntryOptions = {}): CheckpointAuditEntry {
  const workspaceUid = options.workspaceUid ?? WORKSPACE
  const taskId = options.taskId === undefined ? TASK : options.taskId
  const date = options.date ?? '2026-09-05'
  const ordinal = options.ordinal ?? 0
  const seed = options.seed ?? '1'
  const state = options.state ?? 'active'
  const revision = options.revision ?? (state === 'superseded' ? 1 : 0)
  const payload = 'payload' in options
    ? options.payload
    : { task_id: taskId, task: 'Ship the resume view', done: ['drafted'], next: ['review'], blockers: [] }
  if (options.legacy) {
    return {
      locator: { workspace_uid: workspaceUid, task_id: null, date, ordinal, entry_digest: null },
      checkpoint_id: null,
      entry: payload,
      recorded: null,
      state: 'active',
      revision: 0,
      transitions: [],
    }
  }
  const id = checkpointId(seed)
  const entryDigest = digest(seed)
  return {
    locator: { workspace_uid: workspaceUid, task_id: taskId, date, ordinal, entry_digest: entryDigest },
    checkpoint_id: id,
    entry: payload,
    recorded: {
      type: 'worklog.recorded',
      workspace_uid: workspaceUid,
      task_id: taskId ?? TASK,
      checkpoint_id: id,
      date,
      ordinal,
      entry_digest: entryDigest,
      origin: options.origin === undefined ? 'agent-cli-v1' : options.origin,
    },
    state,
    revision,
    transitions: [],
  }
}

function audit(...entries: CheckpointAuditEntry[]): CheckpointAudit {
  return { workspace_uid: WORKSPACE, entries }
}

function select(source: CheckpointAudit, taskId = TASK, workspaceUid = WORKSPACE) {
  return selectTaskResumeFacts(workspaceUid, taskId, { audit: source, ...READ })
}

describe('the latest active record is the one that is read', () => {
  test('a later day wins, whatever order the audit arrived in', () => {
    const facts = select(audit(
      entry({ date: '2026-09-01', ordinal: 0, seed: 'f', payload: { next: ['older'] } }),
      // A deliberately smaller checkpoint id on the NEWER row: ids are identity,
      // never order.
      entry({ date: '2026-09-07', ordinal: 0, seed: '0', payload: { next: ['newest'] } }),
      entry({ date: '2026-09-03', ordinal: 9, seed: 'e', payload: { next: ['middle'] } }),
    ))
    expect(facts.status).toBe('ready')
    expect(facts.next).toEqual(['newest'])
    expect(facts.provenance?.date).toBe('2026-09-07')
    expect(facts.activeRecordCount).toBe(3)
  })

  test('within one day the higher ordinal is later, with no invented time', () => {
    const facts = select(audit(
      entry({ ordinal: 2, seed: 'b', payload: { next: ['second slot'] } }),
      entry({ ordinal: 7, seed: 'c', payload: { next: ['seventh slot'] } }),
      entry({ ordinal: 1, seed: 'd', payload: { next: ['first slot'] } }),
    ))
    expect(facts.next).toEqual(['seventh slot'])
    expect(facts.provenance?.ordinal).toBe(7)
  })

  test('a superseded newest record is excluded, not read as current', () => {
    const facts = select(audit(
      entry({ date: '2026-09-02', seed: 'a', payload: { next: ['still true'] } }),
      entry({ date: '2026-09-06', seed: 'b', state: 'superseded', revision: 1, payload: { next: ['withdrawn'] } }),
    ))
    expect(facts.next).toEqual(['still true'])
    expect(facts.provenance?.date).toBe('2026-09-02')
    expect(facts.activeRecordCount).toBe(1)
    expect(facts.supersededRecordCount).toBe(1)
  })

  test('a Task whose every record was superseded reports no progress, not a stale plan', () => {
    const facts = select(audit(
      entry({ seed: 'a', state: 'superseded', revision: 1, payload: { next: ['withdrawn'] } }),
    ))
    expect(facts.status).toBe('empty')
    expect(facts.next).toEqual([])
    expect(facts.provenance).toBeNull()
    expect(facts.supersededRecordCount).toBe(1)
  })
})

describe('one record, never a blend of several', () => {
  test('a newer record that cleared its blockers is not backfilled from an older one', () => {
    const facts = select(audit(
      entry({ date: '2026-09-02', seed: 'a', payload: { done: ['found cause'], next: ['fix it'], blockers: ['waiting on review'] } }),
      entry({ date: '2026-09-06', seed: 'b', payload: { done: ['fixed'], next: ['ship'], blockers: [] } }),
    ))
    expect(facts.done).toEqual(['fixed'])
    expect(facts.next).toEqual(['ship'])
    // The newer record's silence is the author saying nothing is in the way.
    expect(facts.blockers).toEqual([])
  })

  test('an omitted field is empty and never inherits the previous record', () => {
    const facts = select(audit(
      entry({ date: '2026-09-02', seed: 'a', payload: { next: ['old next'] } }),
      entry({ date: '2026-09-06', seed: 'b', payload: { done: ['closed out'] } }),
    ))
    expect(facts.status).toBe('ready')
    expect(facts.done).toEqual(['closed out'])
    expect(facts.next).toEqual([])
  })
})

describe('the snapshot answers for exactly one Task in one workspace', () => {
  test('another Task, even a newer one, contributes nothing', () => {
    const facts = select(audit(
      entry({ date: '2026-09-02', seed: 'a', payload: { next: ['mine'] } }),
      entry({ date: '2026-09-08', seed: 'b', taskId: OTHER_TASK, payload: { task_id: OTHER_TASK, next: ['theirs'] } }),
    ))
    expect(facts.taskId).toBe(TASK)
    expect(facts.next).toEqual(['mine'])
    expect(facts.activeRecordCount).toBe(1)
  })

  test('another workspace contributes nothing even for the same Task id', () => {
    const foreign = entry({ date: '2026-09-08', seed: 'b', workspaceUid: OTHER_WORKSPACE, payload: { next: ['foreign'] } })
    const facts = select(audit(entry({ date: '2026-09-02', seed: 'a', payload: { next: ['mine'] } }), foreign))
    expect(facts.next).toEqual(['mine'])
    expect(facts.activeRecordCount).toBe(1)
  })

  test('a Task with no record at all says so', () => {
    const facts = select(audit(entry({ taskId: OTHER_TASK, payload: { task_id: OTHER_TASK } })))
    expect(facts.status).toBe('empty')
    expect(facts.provenance).toBeNull()
    expect(facts.version).toBe(`v1:empty:${WORKSPACE}:${TASK}`)
  })
})

describe('an unreadable latest record stays the latest', () => {
  test('nothing readable in the newest entry is stated, not replaced by an older one', () => {
    const facts = select(audit(
      entry({ date: '2026-09-02', seed: 'a', payload: { next: ['older but readable'] } }),
      entry({ date: '2026-09-06', seed: 'b', payload: null }),
    ))
    expect(facts.status).toBe('unreadable')
    expect(facts.unreadableReason).toBe(NO_ENTRY_CONTENT)
    expect(facts.next).toEqual([])
    // The reader is pointed at the broken record, not at a substituted one.
    expect(facts.provenance?.date).toBe('2026-09-06')
    // History is still worth opening, and the count says so.
    expect(facts.activeRecordCount).toBe(2)
  })

  test('an opaque payload keeps every field discoverable', () => {
    const facts = select(audit(entry({ seed: 'b', payload: { unknown_shape: 'kept' } })))
    expect(facts.status).toBe('unreadable')
    expect(facts.unreadableReason).toBe(NO_READABLE_SUMMARY)
    expect(facts.extras).toEqual([{ label: 'unknown_shape', value: 'kept' }])
  })
})

describe('partially readable records are read, and said to be partial', () => {
  test('a value the summary could not render leaves the record partial', () => {
    const facts = select(audit(entry({
      seed: 'b',
      payload: { task_id: TASK, next: ['review the plan'], reviewer: { name: 'unnamed' } },
    })))
    expect(facts.status).toBe('partial')
    expect(facts.next).toEqual(['review the plan'])
    expect(facts.extras).toEqual([{ label: 'reviewer', value: '{"name":"unnamed"}' }])
  })

  test('a payload naming a different Task than its locator is partial, not silently trusted', () => {
    const facts = select(audit(entry({
      seed: 'b',
      payload: { task_id: OTHER_TASK, next: ['filed under the wrong Task'] },
    })))
    expect(facts.status).toBe('partial')
    expect(facts.taskId).toBe(TASK)
    expect(facts.provenance?.binding).toBe('locator')
    expect(facts.provenance?.recordedTaskId).toBe(OTHER_TASK)
  })
})

describe('a legacy row keeps its weaker binding visible', () => {
  test('a row with no Task locator binds through its own payload and declares it', () => {
    const facts = select(audit(entry({
      legacy: true,
      date: '2026-08-20',
      ordinal: 3,
      payload: { task_id: TASK, task: 'Older work', next: ['carry on'] },
    })))
    expect(facts.status).toBe('ready')
    expect(facts.next).toEqual(['carry on'])
    expect(facts.provenance).toMatchObject({
      binding: 'entry-payload',
      checkpointId: null,
      entryDigest: null,
      origin: null,
      recordedTaskTitle: 'Older work',
    })
    expect(facts.version).toBe(
      `v1:ready:${WORKSPACE}:${TASK}:legacy@2026-08-20#3:no-digest:r0`,
    )
  })

  test('a legacy row naming another Task is not borrowed', () => {
    const facts = select(audit(entry({ legacy: true, payload: { task_id: OTHER_TASK, next: ['theirs'] } })))
    expect(facts.status).toBe('empty')
  })

  test('a legacy row with an unreadable payload cannot claim any Task', () => {
    const facts = select(audit(entry({ legacy: true, payload: 'free text from before the schema' })))
    expect(facts.status).toBe('empty')
    expect(facts.activeRecordCount).toBe(0)
  })
})

describe('provenance is carried, never derived', () => {
  test('identity, day, slot, revision and origin come straight from the record', () => {
    const facts = select(audit(entry({
      seed: 'c', date: '2026-09-04', ordinal: 5, origin: null,
    })))
    expect(facts.provenance).toEqual({
      workspaceUid: WORKSPACE,
      taskId: TASK,
      checkpointId: checkpointId('c'),
      entryDigest: digest('c'),
      date: '2026-09-04',
      ordinal: 5,
      revision: 0,
      origin: null,
      binding: 'locator',
      recordedTaskId: TASK,
      recordedTaskTitle: 'Ship the resume view',
    })
  })
})

describe('lifetime states are distinguished, never collapsed into empty', () => {
  test('a read in flight is loading, not an absence of progress', () => {
    const facts = selectTaskResumeFacts(WORKSPACE, TASK, { audit: undefined, isPending: true, error: null })
    expect(facts.status).toBe('loading')
    expect(facts.version).toBe(`v1:loading:${WORKSPACE}:${TASK}`)
  })

  test('a failed read carries its message verbatim and presents no facts', () => {
    const facts = selectTaskResumeFacts(WORKSPACE, TASK, {
      audit: audit(entry({ payload: { next: ['cached from an earlier read'] } })),
      isPending: false,
      error: new Error('checkpoint audit rejected: 503'),
    })
    expect(facts.status).toBe('error')
    expect(facts.errorMessage).toBe('checkpoint audit rejected: 503')
    // A failed read never presents an earlier record as the current one.
    expect(facts.next).toEqual([])
    expect(facts.provenance).toBeNull()
  })

  test('an idle read with no data is empty rather than perpetually loading', () => {
    const facts = selectTaskResumeFacts(WORKSPACE, TASK, { audit: undefined, isPending: false, error: null })
    expect(facts.status).toBe('empty')
  })
})

describe('version is the freshness identity a prepared brief freezes', () => {
  test('a checkpoint-only change invalidates it, with no Task revision involved', () => {
    const before = select(audit(entry({ date: '2026-09-05', seed: 'a', payload: { next: ['first'] } })))
    const after = select(audit(
      entry({ date: '2026-09-05', seed: 'a', payload: { next: ['first'] } }),
      entry({ date: '2026-09-06', seed: 'b', payload: { next: ['second'] } }),
    ))
    expect(before.version).not.toBe(after.version)
    expect(after.version).toBe(
      `v1:ready:${WORKSPACE}:${TASK}:${checkpointId('b')}:${digest('b')}:r0`,
    )
  })

  test('superseding the latest record invalidates it even though no record was added', () => {
    const latest = { date: '2026-09-06', seed: 'b', payload: { next: ['second'] } } as const
    const before = select(audit(entry({ date: '2026-09-05', seed: 'a' }), entry(latest)))
    const after = select(audit(
      entry({ date: '2026-09-05', seed: 'a' }),
      entry({ ...latest, state: 'superseded', revision: 1 }),
    ))
    expect(before.version).not.toBe(after.version)
  })

  test('another Task gaining a checkpoint does not stale this brief', () => {
    const mine = entry({ date: '2026-09-05', seed: 'a' })
    const before = select(audit(mine))
    const after = select(audit(mine, entry({
      date: '2026-09-08', seed: 'b', taskId: OTHER_TASK, payload: { task_id: OTHER_TASK, next: ['theirs'] },
    })))
    expect(before.version).toBe(after.version)
  })

  test('going from no record to a first record invalidates it', () => {
    const before = select(audit())
    const after = select(audit(entry({ seed: 'a' })))
    expect(before.version).not.toBe(after.version)
  })
})

test('the same audit always produces the same facts', () => {
  const source = audit(
    entry({ date: '2026-09-05', seed: 'a', ordinal: 1 }),
    entry({ date: '2026-09-05', seed: 'b', ordinal: 2, payload: { next: ['latest'], extra: 3 } }),
  )
  expect(select(source)).toEqual(select(source))
})

test('the audit key is the one Daily Review already uses', () => {
  expect(taskResumeAuditQueryKey(WORKSPACE)).toEqual(['checkpoint-audit', WORKSPACE])
})
