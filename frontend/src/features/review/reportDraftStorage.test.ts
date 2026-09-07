import { afterEach, describe, expect, test } from 'vitest'

import {
  REPORT_DRAFT_LOCK_NAME,
  REPORT_DRAFT_MAX_CHARS,
  REPORT_DRAFT_MAX_MARKDOWN,
  REPORT_DRAFT_MAX_RECORDS,
  REPORT_DRAFT_STORAGE_KEY,
  createReportDraftStore,
  type DraftStorage,
  type ExclusiveLockRunner,
  type ReportDraftInput,
} from './reportDraftStorage'

const WORKSPACE_A = '11111111-1111-4111-8111-111111111111'
const WORKSPACE_B = '22222222-2222-4222-8222-222222222222'
const DATE_A = '2026-09-06'
const DATE_B = '2026-09-07'
const DIGEST_A = `sha256:${'a'.repeat(64)}`
const DIGEST_B = `sha256:${'b'.repeat(64)}`
const GENERATED_AT = '2026-09-06T11:00:00Z'
const UPDATED_AT = '2026-09-06T12:00:00.000Z'

function serialLock(): ExclusiveLockRunner {
  let tail = Promise.resolve()
  return async (_name, run) => {
    const ready = tail
    let release!: () => void
    tail = new Promise((resolve) => {
      release = resolve
    })
    await ready
    try {
      return await run()
    } finally {
      release()
    }
  }
}

function memoryStorage(initial?: string): DraftStorage & { data: Map<string, string> } {
  const data = new Map<string, string>()
  if (initial !== undefined) data.set(REPORT_DRAFT_STORAGE_KEY, initial)
  return {
    data,
    getItem(key) {
      return data.has(key) ? data.get(key) as string : null
    },
    setItem(key, value) {
      data.set(key, value)
    },
  }
}

function input(overrides: Partial<ReportDraftInput> = {}): ReportDraftInput {
  return {
    workspaceUid: WORKSPACE_A,
    date: DATE_A,
    template: 'daily-v1',
    sourceDigest: DIGEST_A,
    baseGeneratedAt: GENERATED_AT,
    baseMarkdown: '# base',
    markdown: '# edited',
    ...overrides,
  }
}

function coordinate(overrides: Partial<ReportDraftInput> = {}) {
  const value = input(overrides)
  return {
    workspaceUid: value.workspaceUid,
    date: value.date,
    template: value.template,
  }
}

function store(storage: DraftStorage, lock: ExclusiveLockRunner = serialLock()) {
  return createReportDraftStore({
    storage: () => storage,
    withExclusiveLock: lock,
    now: () => new Date(UPDATED_AT),
  })
}

afterEach(() => {
  expect(window.localStorage.getItem(REPORT_DRAFT_STORAGE_KEY)).toBeNull()
})

describe('report draft buffer', () => {
  test('does not access storage until an operation runs', () => {
    let accessed = 0
    createReportDraftStore({
      storage: () => {
        accessed += 1
        return memoryStorage()
      },
      withExclusiveLock: serialLock(),
    })
    expect(accessed).toBe(0)
  })

  test('round-trips a draft and preserves source identity', async () => {
    const drafts = store(memoryStorage())
    const saved = await drafts.save(input(), null)
    expect(saved).toEqual({
      ok: true,
      value: { ...input(), localRevision: 1, updatedAt: UPDATED_AT },
    })
    const loaded = await drafts.load(coordinate())
    expect(loaded).toEqual(saved)
    if (loaded.ok) {
      expect(loaded.value?.sourceDigest).toBe(DIGEST_A)
      expect(loaded.value?.baseMarkdown).toBe('# base')
    }
  })

  test('isolates drafts by workspace and day', async () => {
    const drafts = store(memoryStorage())
    await drafts.save(input(), null)
    await drafts.save(input({ workspaceUid: WORKSPACE_B, markdown: 'b' }), null)
    await drafts.save(input({ date: DATE_B, markdown: 'day' }), null)
    const first = await drafts.load(coordinate())
    const otherWorkspace = await drafts.load(coordinate({ workspaceUid: WORKSPACE_B }))
    const otherDay = await drafts.load(coordinate({ date: DATE_B }))
    expect(first.ok && first.value?.markdown).toBe('# edited')
    expect(otherWorkspace.ok && otherWorkspace.value?.markdown).toBe('b')
    expect(otherDay.ok && otherDay.value?.markdown).toBe('day')
  })

  test('accepts real server timestamps and rejects invalid wire coordinates without writing', async () => {
    const storage = memoryStorage()
    const drafts = store(storage)
    const saved = await drafts.save(input(), null)
    expect(saved.ok).toBe(true)
    const before = storage.data.get(REPORT_DRAFT_STORAGE_KEY)
    for (const overrides of [
      { sourceDigest: 'a'.repeat(64) },
      { workspaceUid: '11111111-1111-4111-7111-111111111111' },
      { baseGeneratedAt: '2026-02-30T11:00:00Z' },
      { baseGeneratedAt: '2026-09-06T25:00:00Z' },
      { date: '0000-01-01' },
    ]) {
      expect(await drafts.save(input(overrides), 1)).toEqual({ ok: false, code: 'invalid_input' })
      expect(storage.data.get(REPORT_DRAFT_STORAGE_KEY)).toBe(before)
    }
    expect((await drafts.save(input({ baseGeneratedAt: `${GENERATED_AT.slice(0, -1)}.000Z` }), 1)).ok).toBe(true)
    expect((await drafts.save(input({ date: '0001-01-01' }), null)).ok).toBe(true)
  })

  test('keeps fourteen records and refuses a fifteenth without eviction', async () => {
    const storage = memoryStorage()
    const drafts = store(storage)
    for (let day = 1; day <= REPORT_DRAFT_MAX_RECORDS; day += 1) {
      const date = `2026-01-${String(day).padStart(2, '0')}`
      const saved = await drafts.save(input({ date, markdown: date }), null)
      expect(saved.ok).toBe(true)
    }
    const overflow = await drafts.save(input({ date: '2026-02-01', markdown: 'extra' }), null)
    expect(overflow).toEqual({ ok: false, code: 'capacity' })
    expect(JSON.parse(storage.data.get(REPORT_DRAFT_STORAGE_KEY) as string).records).toHaveLength(14)
    const kept = await drafts.load(coordinate({ date: '2026-01-01' }))
    expect(kept.ok && kept.value?.markdown).toBe('2026-01-01')
    const missing = await drafts.load(coordinate({ date: '2026-02-01' }))
    expect(missing).toEqual({ ok: true, value: null })
  })

  test('increments matching revisions and conflicts otherwise', async () => {
    const drafts = store(memoryStorage())
    const first = await drafts.save(input(), null)
    expect(first.ok && first.value.localRevision).toBe(1)
    const staleCreate = await drafts.save(input({ markdown: 'again' }), null)
    expect(staleCreate).toEqual({ ok: false, code: 'conflict' })
    const second = await drafts.save(input({ markdown: 'v2' }), 1)
    expect(second.ok && second.value.localRevision).toBe(2)
    const stale = await drafts.save(input({ markdown: 'v3' }), 1)
    expect(stale).toEqual({ ok: false, code: 'conflict' })
    const loaded = await drafts.load(coordinate())
    expect(loaded.ok && loaded.value?.markdown).toBe('v2')
  })

  test('removes only the matching revision', async () => {
    const drafts = store(memoryStorage())
    await drafts.save(input(), null)
    await drafts.save(input({ markdown: 'v2' }), 1)
    expect(await drafts.remove(coordinate(), 1)).toEqual({ ok: false, code: 'conflict' })
    expect(await drafts.remove(coordinate({ date: DATE_B }), 2)).toEqual({ ok: false, code: 'not_found' })
    expect(await drafts.remove(coordinate(), 2)).toEqual({ ok: true, value: true })
    expect(await drafts.load(coordinate())).toEqual({ ok: true, value: null })
  })

  test('concurrent saves with the same expected revision keep only one winner', async () => {
    const drafts = store(memoryStorage())
    await drafts.save(input(), null)
    const [left, right] = await Promise.all([
      drafts.save(input({ markdown: 'left' }), 1),
      drafts.save(input({ markdown: 'right' }), 1),
    ])
    const codes = [left, right].map((result) => (result.ok ? 'ok' : result.code)).sort()
    expect(codes).toEqual(['conflict', 'ok'])
    const loaded = await drafts.load(coordinate())
    expect(loaded.ok && loaded.value?.localRevision).toBe(2)
    expect(loaded.ok && (loaded.value?.markdown === 'left' || loaded.value?.markdown === 'right')).toBe(true)
  })

  test('load still works when the exclusive lock is unavailable', async () => {
    const storage = memoryStorage()
    const seeded = store(storage)
    await seeded.save(input(), null)
    const lockedOut = store(storage, async () => {
      throw new Error('lock-refused-canary')
    })
    const loaded = await lockedOut.load(coordinate())
    expect(loaded.ok && loaded.value?.markdown).toBe('# edited')
    const saved = await lockedOut.save(input({ markdown: 'nope' }), 1)
    expect(saved).toEqual({ ok: false, code: 'storage_unavailable' })
    expect(JSON.stringify(saved)).not.toContain('lock-refused-canary')
    expect(await seeded.load(coordinate())).toEqual(loaded)
  })
})

describe('invalid storage and inputs stay in place', () => {
  test('rejects malformed extra oversize stored envelopes without deleting them', async () => {
    const cases = [
      '{',
      JSON.stringify({ version: 2, records: [] }),
      JSON.stringify({ version: 1, records: [], extra: true }),
      JSON.stringify({
        version: 1,
        records: [
          { ...input(), localRevision: 1, updatedAt: UPDATED_AT },
          { ...input(), markdown: 'dup', localRevision: 2, updatedAt: UPDATED_AT },
        ],
      }),
      JSON.stringify({
        version: 1,
        records: [{ ...input(), localRevision: 1, updatedAt: UPDATED_AT, extra: true }],
      }),
      'x'.repeat(REPORT_DRAFT_MAX_CHARS + 1),
    ]
    for (const raw of cases) {
      const storage = memoryStorage(raw)
      const drafts = store(storage)
      expect(await drafts.load(coordinate())).toEqual({ ok: false, code: 'invalid_storage' })
      expect(await drafts.save(input(), null)).toEqual({ ok: false, code: 'invalid_storage' })
      expect(storage.data.get(REPORT_DRAFT_STORAGE_KEY)).toBe(raw)
    }
  })

  test('rejects malformed extra and oversize inputs without writing', async () => {
    const storage = memoryStorage()
    const drafts = store(storage)
    const invalids: unknown[] = [
      { ...input(), extra: true },
      { ...input(), workspaceUid: NIL_OR_BAD() },
      { ...input(), date: '2026-02-30' },
      { ...input(), sourceDigest: DIGEST_A.toUpperCase() },
      { ...input(), markdown: 'm'.repeat(REPORT_DRAFT_MAX_MARKDOWN + 1) },
      { ...input(), template: 'weekly-v1' },
    ]
    for (const value of invalids) {
      const result = await drafts.save(value as ReportDraftInput, null)
      expect(result).toEqual({ ok: false, code: 'invalid_input' })
    }
    expect(storage.data.has(REPORT_DRAFT_STORAGE_KEY)).toBe(false)
    expect(await drafts.load(coordinate())).toEqual({ ok: true, value: null })
  })

  test('quota and security failures retain the caller input and stored bytes', async () => {
    const quota = memoryStorage()
    quota.setItem = () => {
      const error = new Error('quota-canary')
      error.name = 'QuotaExceededError'
      throw error
    }
    const quotaStore = store(quota)
    const payload = input({ markdown: 'keep-me' })
    const quotaResult = await quotaStore.save(payload, null)
    expect(quotaResult).toEqual({ ok: false, code: 'storage_write_failed' })
    expect(JSON.stringify(quotaResult)).not.toContain('quota-canary')
    expect(payload.markdown).toBe('keep-me')
    expect(quota.data.has(REPORT_DRAFT_STORAGE_KEY)).toBe(false)

    const security = memoryStorage(JSON.stringify({ version: 1, records: [] }))
    const before = security.data.get(REPORT_DRAFT_STORAGE_KEY)
    security.getItem = () => {
      const error = new Error('security-canary')
      error.name = 'SecurityError'
      throw error
    }
    const securityStore = store(security)
    const securityResult = await securityStore.load(coordinate())
    expect(securityResult).toEqual({ ok: false, code: 'storage_unavailable' })
    expect(JSON.stringify(securityResult)).not.toContain('security-canary')
    expect(security.data.get(REPORT_DRAFT_STORAGE_KEY)).toBe(before)
  })

  test('refuses an exhausted revision without rewriting storage', async () => {
    const row = { ...input(), localRevision: Number.MAX_SAFE_INTEGER, updatedAt: UPDATED_AT }
    const raw = JSON.stringify({ version: 1, records: [row] })
    const storage = memoryStorage(raw)
    const drafts = store(storage)
    expect(await drafts.save(input({ markdown: 'nope' }), Number.MAX_SAFE_INTEGER)).toEqual({
      ok: false,
      code: 'revision_exhausted',
    })
    expect(storage.data.get(REPORT_DRAFT_STORAGE_KEY)).toBe(raw)
  })

  test('writes share one origin-wide lock name', async () => {
    const names: string[] = []
    const inner = serialLock()
    const lock: ExclusiveLockRunner = async (name, run) => {
      names.push(name)
      return inner(name, run)
    }
    const drafts = store(memoryStorage(), lock)
    await drafts.save(input(), null)
    await drafts.remove(coordinate(), 1)
    expect(names).toEqual([REPORT_DRAFT_LOCK_NAME, REPORT_DRAFT_LOCK_NAME])
  })

  test('does not auto-replace a mismatched source digest', async () => {
    const drafts = store(memoryStorage())
    await drafts.save(input({ sourceDigest: DIGEST_A, markdown: 'local' }), null)
    const loaded = await drafts.load(coordinate())
    expect(loaded.ok && loaded.value?.sourceDigest).toBe(DIGEST_A)
    expect(loaded.ok && loaded.value?.markdown).toBe('local')
    const unchanged = await drafts.load(coordinate())
    expect(unchanged).toEqual(loaded)
    expect(unchanged.ok && unchanged.value?.sourceDigest).not.toBe(DIGEST_B)
  })
})

function NIL_OR_BAD() {
  return '00000000-0000-0000-0000-000000000000'
}
