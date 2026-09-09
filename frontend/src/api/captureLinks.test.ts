import { afterEach, expect, test, vi } from 'vitest'
import { capture, jsonResponse, task } from '../test/fixtures'
import { ApiError } from './transport'
import {
  CAPTURE_UNLINK_FALLBACK_MESSAGE,
  CaptureUnlinkError,
  UNLINK_NOT_AVAILABLE,
  UNLINK_UNREADABLE,
  captureUnlinkPath,
  classifyCaptureUnlinkFailure,
  describeCaptureUnlinkFailure,
  unlinkCaptureTask,
} from './captureLinks'

/**
 * The published R22 route, exercised against a network mock of the exact contract.
 * The backend lands on its own branch; this file is the frontend half of that contract.
 */

const KEY = 'workstack:11111111-2222-4333-8444-555555555555'
const LINKED = { ...capture, status: 'linked' as const, linked_task_ids: [task.id], revision: 4 }
const UNLINKED = { ...capture, status: 'inbox' as const, linked_task_ids: [], revision: 5 }

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stubFetch(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    return handler(url, init)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function unlink(overrides: Partial<Parameters<typeof unlinkCaptureTask>[0]> = {}) {
  return unlinkCaptureTask({
    captureId: LINKED.id,
    taskId: task.id,
    revision: 4,
    idempotencyKey: KEY,
    ...overrides,
  })
}

/** A refusal is the only expected end of these calls; a success here is a test failure. */
async function failureOf(run: () => Promise<unknown>) {
  const outcome = await run().then(() => null, (error: unknown) => classifyCaptureUnlinkFailure(error))
  if (outcome === null) throw new Error('the unlink request unexpectedly succeeded')
  return outcome
}

function mutation(mock: ReturnType<typeof stubFetch>) {
  const call = mock.mock.calls.find(([input]) => String(input).includes('/unlink'))
  if (!call) throw new Error('no unlink request was made')
  const init = call[1] as RequestInit
  return {
    url: String(call[0]),
    init,
    headers: init.headers as Record<string, string>,
    body: JSON.parse(String(init.body)) as Record<string, unknown>,
  }
}

test('posts exactly the published path, method, body and headers', async () => {
  const mock = stubFetch(() => jsonResponse({ data: UNLINKED, meta: { duplicate: false } }))

  const outcome = await unlink()

  const request = mutation(mock)
  expect(request.url).toBe('/api/v1/captures/C-0001/unlink')
  expect(request.url).toBe(captureUnlinkPath(LINKED.id))
  expect(request.init.method).toBe('POST')
  expect(request.init.credentials).toBe('same-origin')
  // Exactly the two contract fields, and the DISPLAYED revision, not a re-read one.
  expect(request.body).toEqual({ task_id: task.id, revision: 4 })
  expect(request.headers['Idempotency-Key']).toBe(KEY)
  expect(request.headers['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect(outcome.duplicate).toBe(false)
  expect(outcome.capture.linked_task_ids).toEqual([])
})

test('an already absent link at the correct revision is a duplicate success', async () => {
  stubFetch(() => jsonResponse({ data: LINKED, meta: { duplicate: true } }))

  await expect(unlink()).resolves.toMatchObject({ duplicate: true })
})

test('never fetches the Capture to retry with a newer revision after a conflict', async () => {
  const mock = stubFetch(() => jsonResponse(
    { error: { code: 'revision_conflict', message: 'capture revision does not match' } },
    409,
  ))

  const failure = await failureOf(() => unlink())

  expect(failure).toEqual({
    kind: 'conflict',
    code: 'revision_conflict',
    message: 'This capture changed since it was shown, so nothing was removed. Refresh the Task and try again.',
  })
  // One POST, and no capture read at all: the stale view is reported, not worked around.
  const requests = mock.mock.calls.map(([input]) => String(input)).filter((url) => !url.includes('/session'))
  expect(requests).toEqual(['/api/v1/captures/C-0001/unlink'])
  // Backend prose never reaches the caller's copy.
  expect(JSON.stringify(failure)).not.toContain('capture revision does not match')
})

test('a 404 is a determinate refusal with its own closed sentence', async () => {
  stubFetch(() => jsonResponse({ error: { code: 'not_found', message: 'capture not found' } }, 404))

  const failure = await failureOf(() => unlink())

  expect(failure).toEqual({
    kind: 'refused',
    code: 'not_found',
    message: 'This capture or Task is no longer available, so nothing was removed.',
  })
})

test('an unpublished refusal code keeps the one fallback sentence', async () => {
  stubFetch(() => jsonResponse({ error: { code: 'teapot', message: 'server prose' } }, 400))

  const failure = await failureOf(() => unlink())

  expect(failure.kind).toBe('refused')
  expect(failure.message).toBe(CAPTURE_UNLINK_FALLBACK_MESSAGE)
  expect(failure.message).not.toContain('server prose')
})

test('a failed request is the unknown outcome, not a refusal', async () => {
  stubFetch(() => Promise.reject(new TypeError('Failed to fetch')))

  const failure = await failureOf(() => unlink())

  expect(failure.kind).toBe('ambiguous')
  expect(failure.code).toBe(UNLINK_UNREADABLE)
})

test('an unreadable 200 body is the unknown outcome', async () => {
  stubFetch(() => Promise.resolve(new Response('<html>gateway</html>', {
    status: 200,
    headers: { 'Content-Type': 'text/html' },
  })))

  await expect(unlink()).rejects.toBeInstanceOf(CaptureUnlinkError)
  const failure = await failureOf(() => unlink())
  expect(failure.kind).toBe('ambiguous')
})

test('a well formed body returned as 202 is not admitted as a settled removal', async () => {
  stubFetch(() => jsonResponse({ data: UNLINKED, meta: { duplicate: false } }, 202))

  const failure = await failureOf(() => unlink())

  expect(failure.kind).toBe('ambiguous')
})

test('a missing meta.duplicate is the unknown outcome, not a silent success', async () => {
  stubFetch(() => jsonResponse({ data: UNLINKED, meta: {} }))

  const failure = await failureOf(() => unlink())

  expect(failure.kind).toBe('ambiguous')
})

test('a body that is not the Capture projection is the unknown outcome', async () => {
  stubFetch(() => jsonResponse({ data: { removed: true }, meta: { duplicate: false } }))

  const failure = await failureOf(() => unlink())

  expect(failure.kind).toBe('ambiguous')
})

test('a 500 is the unknown outcome because no published refusal reaches it', async () => {
  stubFetch(() => jsonResponse({ error: { code: 'internal_error', message: 'boom' } }, 500))

  const failure = await failureOf(() => unlink())

  expect(failure.kind).toBe('ambiguous')
  expect(failure.message).not.toContain('boom')
})

test('a malformed intent is refused before any request is made', async () => {
  const mock = stubFetch(() => jsonResponse({ data: UNLINKED, meta: { duplicate: false } }))

  const failures = await Promise.all([
    failureOf(() => unlink({ revision: -1 })),
    failureOf(() => unlink({ revision: 1.5 })),
    failureOf(() => unlink({ revision: true as unknown as number })),
    failureOf(() => unlink({ taskId: '' })),
    failureOf(() => unlink({ captureId: '' })),
    failureOf(() => unlink({ idempotencyKey: '' })),
  ])

  // Determinate: nothing was sent, so nothing can have been written.
  expect(failures.map((entry) => entry.kind)).toEqual(Array(6).fill('refused'))
  expect(failures.every((entry) => entry.code === UNLINK_NOT_AVAILABLE)).toBe(true)
  expect(mock.mock.calls.filter(([input]) => String(input).includes('/unlink'))).toHaveLength(0)
})

test('a backend code that names an Object prototype member cannot render a function', () => {
  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
    expect(describeCaptureUnlinkFailure(code)).toBe(CAPTURE_UNLINK_FALLBACK_MESSAGE)
    const classified = classifyCaptureUnlinkFailure(new ApiError(400, code, 'prose'))
    expect(classified.message).toBe(CAPTURE_UNLINK_FALLBACK_MESSAGE)
    expect(typeof classified.message).toBe('string')
  }
})

test('an unrecognised thrown value is ambiguous rather than a claimed refusal', () => {
  expect(classifyCaptureUnlinkFailure(undefined).kind).toBe('ambiguous')
  expect(classifyCaptureUnlinkFailure(new Error('boom')).kind).toBe('ambiguous')
  expect(classifyCaptureUnlinkFailure(new CaptureUnlinkError(UNLINK_NOT_AVAILABLE)).kind).toBe('refused')
})

/**
 * R33: the optional Undo receipt hint.
 *
 * It is additive metadata on a route whose success semantics are already published, so
 * every case below has the same second half: whatever the hint does or does not say, the
 * removal the server confirmed stays confirmed.
 */

const RECEIPT = '9f8e7d6c-5b4a-4392-8271-0a1b2c3d4e5f'

test('a canonical receipt hint is read alongside the legacy duplicate metadata', async () => {
  stubFetch(() => jsonResponse({ data: UNLINKED, meta: { duplicate: false, undo_receipt_id: RECEIPT } }))

  const outcome = await unlink()

  expect(outcome).toMatchObject({ duplicate: false, undoReceiptId: RECEIPT })
  expect(outcome.capture.revision).toBe(5)
})

test('a legacy response without the additive field is still a plain success', async () => {
  stubFetch(() => jsonResponse({ data: UNLINKED, meta: { duplicate: false } }))

  const outcome = await unlink()

  expect(outcome.duplicate).toBe(false)
  expect(outcome.undoReceiptId).toBeUndefined()
})

test('a malformed hint is dropped and never relabels the confirmed removal as unknown', async () => {
  const malformed: unknown[] = [
    'not-a-uuid',
    RECEIPT.toUpperCase(),
    `{${RECEIPT}}`,
    ` ${RECEIPT}`,
    `${RECEIPT}\n`,
    RECEIPT.replace(/-/g, ''),
    42,
    null,
    { id: RECEIPT },
    [RECEIPT],
  ]
  for (const hint of malformed) {
    stubFetch(() => jsonResponse({ data: UNLINKED, meta: { duplicate: false, undo_receipt_id: hint } }))

    const outcome = await unlink()

    // The removal is still a settled success: only the unusable hint is discarded.
    expect(outcome.duplicate).toBe(false)
    expect(outcome.undoReceiptId).toBeUndefined()
    vi.unstubAllGlobals()
  }
})

test('the hint is reported as read on a duplicate, and is not invented on one either', async () => {
  stubFetch(() => jsonResponse({ data: LINKED, meta: { duplicate: true } }))

  const outcome = await unlink()

  expect(outcome.duplicate).toBe(true)
  expect(outcome.undoReceiptId).toBeUndefined()
})

test('an inherited undo_receipt_id cannot be read off the metadata prototype', async () => {
  // `JSON.parse` gives a plain object, so a `__proto__` key in the wire body is inert
  // data. The own-property test is what keeps a polluted global prototype out too.
  stubFetch(() => jsonResponse({
    data: UNLINKED,
    meta: JSON.parse(`{"duplicate": false, "__proto__": {"undo_receipt_id": "${RECEIPT}"}}`) as unknown,
  }))

  const outcome = await unlink()

  expect(outcome.undoReceiptId).toBeUndefined()
})
