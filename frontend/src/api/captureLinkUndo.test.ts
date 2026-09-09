import { afterEach, expect, test, vi } from 'vitest'
import { capture, jsonResponse, task } from '../test/fixtures'
import { CAPTURE_UNLINK_MESSAGES } from './captureLinks'
import { ApiError } from './transport'
import {
  CAPTURE_UNDO_UNLINK_FALLBACK_MESSAGE,
  CAPTURE_UNDO_UNLINK_MESSAGES,
  CaptureUndoUnlinkError,
  UNDO_UNLINK_NOT_AVAILABLE,
  UNDO_UNLINK_UNREADABLE,
  captureUndoUnlinkPath,
  classifyCaptureUndoUnlinkFailure,
  describeCaptureUndoUnlinkFailure,
  undoCaptureUnlink,
} from './captureLinkUndo'

/**
 * The published R33 Undo route, exercised against a network mock of the exact contract.
 * The owner lands on its own branch; this file is the frontend half of that wire.
 */

const KEY = 'workstack:99999999-8888-4777-8666-555555555555'
const RECEIPT = '9f8e7d6c-5b4a-4392-8271-0a1b2c3d4e5f'
/** The post-unlink row: revision 5, link gone. Undo answers with revision 6, link back. */
const RESTORED = { ...capture, status: 'linked' as const, linked_task_ids: [task.id], revision: 6 }

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

function undo(overrides: Partial<Parameters<typeof undoCaptureUnlink>[0]> = {}) {
  return undoCaptureUnlink({
    captureId: capture.id,
    receiptId: RECEIPT,
    revision: 5,
    taskId: task.id,
    idempotencyKey: KEY,
    ...overrides,
  })
}

/** A refusal is the only expected end of these calls; a success here is a test failure. */
async function failureOf(run: () => Promise<unknown>) {
  const outcome = await run().then(
    () => null,
    (error: unknown) => classifyCaptureUndoUnlinkFailure(error),
  )
  if (outcome === null) throw new Error('the undo request unexpectedly succeeded')
  return outcome
}

function undoRequests(mock: ReturnType<typeof stubFetch>) {
  return mock.mock.calls.map(([input]) => String(input)).filter((url) => url.includes('/undo-unlink'))
}

function mutation(mock: ReturnType<typeof stubFetch>) {
  const call = mock.mock.calls.find(([input]) => String(input).includes('/undo-unlink'))
  if (!call) throw new Error('no undo request was made')
  const init = call[1] as RequestInit
  return {
    url: String(call[0]),
    init,
    headers: init.headers as Record<string, string>,
    body: JSON.parse(String(init.body)) as Record<string, unknown>,
  }
}

test('posts exactly the published path, method, body and headers', async () => {
  const mock = stubFetch(() => jsonResponse({ data: RESTORED, meta: { duplicate: false } }))

  const outcome = await undo()

  const request = mutation(mock)
  expect(request.url).toBe('/api/v1/captures/C-0001/undo-unlink')
  expect(request.url).toBe(captureUndoUnlinkPath(capture.id))
  expect(request.init.method).toBe('POST')
  expect(request.init.credentials).toBe('same-origin')
  // Exactly the two contract fields, and the POST-UNLINK revision, not a re-read one.
  expect(request.body).toEqual({ receipt_id: RECEIPT, revision: 5 })
  expect(request.headers['Idempotency-Key']).toBe(KEY)
  expect(request.headers['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect(outcome.capture.linked_task_ids).toEqual([task.id])
  expect(outcome.capture.revision).toBe(6)
})

test('no prior status, task id or snapshot is ever added to the body', async () => {
  const mock = stubFetch(() => jsonResponse({ data: RESTORED, meta: { duplicate: false } }))

  await undo()

  expect(Object.keys(mutation(mock).body).sort()).toEqual(['receipt_id', 'revision'])
  // The Task the caller wants back is a recognition input; the owner reads it from the
  // receipt, so it must not appear anywhere in the request either.
  expect(String((mutation(mock).init as RequestInit).body)).not.toContain(task.id)
})

test('one explicit intent is exactly one POST when the reply is lost', async () => {
  const mock = stubFetch(() => Promise.reject(new TypeError('Failed to fetch')))

  const failure = await failureOf(() => undo())

  expect(failure.kind).toBe('ambiguous')
  expect(failure.code).toBe(UNDO_UNLINK_UNREADABLE)
  // The shared transport would replay an idempotent mutation on its own. This route
  // declines that: the reader is told it is unconfirmed and decides what happens next.
  expect(undoRequests(mock)).toHaveLength(1)
})

test('a 403 is answered once, never resent under a refreshed session token', async () => {
  const mock = stubFetch(() => jsonResponse(
    { error: { code: 'invalid_request', message: 'stale csrf nonce' } },
    403,
  ))

  const failure = await failureOf(() => undo())

  expect(failure.kind).toBe('refused')
  expect(failure.message).toBe(CAPTURE_UNDO_UNLINK_MESSAGES.invalid_request)
  // No implicit token-refresh resend: one click, one mutation.
  expect(undoRequests(mock)).toHaveLength(1)
})

test('a 200 that does not prove the requested restoration is the unknown outcome', async () => {
  const unrecognised = [
    // A schema-valid projection of a different Capture entirely.
    { label: 'another capture', data: { ...RESTORED, id: 'C-0009' } },
    // The pre-Undo row: this is what the removal already returned, not the restore.
    { label: 'the revision that was sent', data: { ...RESTORED, revision: 5 } },
    // Past the write that was asked for: something else moved this Capture.
    { label: 'a revision beyond the expected one', data: { ...RESTORED, revision: 7 } },
    // Right Capture, right revision, and the link this Undo exists for is still absent.
    { label: 'the intended task still unlinked', data: { ...RESTORED, linked_task_ids: ['T-0002'] } },
  ]
  for (const { label, data } of unrecognised) {
    const mock = stubFetch(() => jsonResponse({ data, meta: { duplicate: false } }))

    const failure = await failureOf(() => undo())

    expect(failure, label).toEqual({
      kind: 'ambiguous',
      code: UNDO_UNLINK_UNREADABLE,
      message: CAPTURE_UNDO_UNLINK_MESSAGES[UNDO_UNLINK_UNREADABLE],
    })
    // Ambiguous, not a second attempt: the same intent stays retryable under its own key.
    expect(undoRequests(mock), label).toHaveLength(1)
    vi.unstubAllGlobals()
  }
})

test('the recognised 200 is the requested capture at the next revision, task linked', async () => {
  stubFetch(() => jsonResponse({ data: RESTORED, meta: { duplicate: false } }))

  const outcome = await undo()

  expect(outcome.capture.id).toBe(capture.id)
  expect(outcome.capture.revision).toBe(6)
  expect(outcome.capture.linked_task_ids).toContain(task.id)
})

test('never fetches the Capture to retry with a newer revision after a conflict', async () => {
  const mock = stubFetch(() => jsonResponse(
    { error: { code: 'revision_conflict', message: 'capture revision does not match' } },
    409,
  ))

  const failure = await failureOf(() => undo())

  expect(failure).toEqual({
    kind: 'conflict',
    code: 'revision_conflict',
    message: CAPTURE_UNDO_UNLINK_MESSAGES.revision_conflict,
  })
  // One POST, and no capture read at all: the stale view is reported, not worked around.
  const requests = mock.mock.calls.map(([input]) => String(input)).filter((url) => !url.includes('/session'))
  expect(requests).toEqual(['/api/v1/captures/C-0001/undo-unlink'])
  expect(JSON.stringify(failure)).not.toContain('capture revision does not match')
})

test('the owner post-image conflict is a determinate refusal with its own sentence', async () => {
  stubFetch(() => jsonResponse(
    { error: { code: 'capture_unlink_undo_conflict', message: 'digest mismatch on row' } },
    409,
  ))

  const failure = await failureOf(() => undo())

  expect(failure.kind).toBe('refused')
  expect(failure.code).toBe('capture_unlink_undo_conflict')
  expect(failure.message).toBe(
    'This capture no longer matches the removal that was undone, so the task link was not restored.',
  )
  expect(failure.message).not.toContain('digest mismatch')
})

test('a missing or foreign receipt is a closed not-found, never a guessed restoration', async () => {
  stubFetch(() => jsonResponse({ error: { code: 'not_found', message: 'receipt not found' } }, 404))

  const failure = await failureOf(() => undo())

  expect(failure.kind).toBe('refused')
  expect(failure.message).toBe('That removal can no longer be undone, so the task link was not restored.')
})

test('an idempotency conflict is refused and says nothing was restored', async () => {
  stubFetch(() => jsonResponse({ error: { code: 'idempotency_conflict', message: 'x' } }, 409))

  const failure = await failureOf(() => undo())

  expect(failure.kind).toBe('refused')
  expect(failure.message).toBe(
    'A different request already used this attempt, so the task link was not restored.',
  )
})

test('an unpublished refusal code keeps the one fallback sentence', async () => {
  stubFetch(() => jsonResponse({ error: { code: 'teapot', message: 'server prose' } }, 400))

  const failure = await failureOf(() => undo())

  expect(failure.kind).toBe('refused')
  expect(failure.message).toBe(CAPTURE_UNDO_UNLINK_FALLBACK_MESSAGE)
  expect(failure.message).not.toContain('server prose')
})

test('this route never renders the unlink copy for its own outcomes', () => {
  const unlinkSentences = new Set(Object.values(CAPTURE_UNLINK_MESSAGES))
  const undoSentences = Object.values(CAPTURE_UNDO_UNLINK_MESSAGES)

  expect(undoSentences.some((sentence) => unlinkSentences.has(sentence))).toBe(false)
  expect(CAPTURE_UNDO_UNLINK_FALLBACK_MESSAGE).not.toBe(
    'The task link could not be removed, and nothing was changed.',
  )
  // Every sentence this surface can show is about restoring, not about removing.
  expect(undoSentences.every((sentence) => /restored|undone/i.test(sentence))).toBe(true)
})

test('a failed request is the unknown outcome, not a refusal', async () => {
  stubFetch(() => Promise.reject(new TypeError('Failed to fetch')))

  const failure = await failureOf(() => undo())

  expect(failure.kind).toBe('ambiguous')
  expect(failure.code).toBe(UNDO_UNLINK_UNREADABLE)
})

test('an unreadable 200 body is the unknown outcome', async () => {
  stubFetch(() => Promise.resolve(new Response('<html>gateway</html>', {
    status: 200,
    headers: { 'Content-Type': 'text/html' },
  })))

  await expect(undo()).rejects.toBeInstanceOf(CaptureUndoUnlinkError)
  expect((await failureOf(() => undo())).kind).toBe('ambiguous')
})

test('a well formed body returned as 202 is not admitted as a settled restoration', async () => {
  stubFetch(() => jsonResponse({ data: RESTORED, meta: { duplicate: false } }, 202))

  expect((await failureOf(() => undo())).kind).toBe('ambiguous')
})

test('a body that is not the Capture projection is the unknown outcome', async () => {
  stubFetch(() => jsonResponse({ data: { restored: true }, meta: { duplicate: false } }))

  expect((await failureOf(() => undo())).kind).toBe('ambiguous')
})

test('a 500 is the unknown outcome because no published refusal reaches it', async () => {
  stubFetch(() => jsonResponse({ error: { code: 'internal_error', message: 'boom' } }, 500))

  const failure = await failureOf(() => undo())

  expect(failure.kind).toBe('ambiguous')
  expect(failure.message).not.toContain('boom')
})

test('a same-key replay of a settled Undo is still a confirmed restoration', async () => {
  // The owner freezes the first outcome; the replay carries the recorded projection.
  stubFetch(() => jsonResponse({ data: RESTORED, meta: { duplicate: true } }))

  await expect(undo()).resolves.toMatchObject({ capture: { revision: 6 } })
})

test('a malformed intent is refused before any request is made', async () => {
  const mock = stubFetch(() => jsonResponse({ data: RESTORED, meta: { duplicate: false } }))

  const failures = await Promise.all([
    failureOf(() => undo({ revision: -1 })),
    failureOf(() => undo({ revision: 1.5 })),
    failureOf(() => undo({ revision: true as unknown as number })),
    failureOf(() => undo({ receiptId: '' })),
    failureOf(() => undo({ receiptId: 'not-a-uuid' })),
    failureOf(() => undo({ receiptId: RECEIPT.toUpperCase() })),
    failureOf(() => undo({ captureId: '' })),
    failureOf(() => undo({ idempotencyKey: '' })),
    failureOf(() => undo({ taskId: '' })),
  ])

  // Determinate: nothing was sent, so nothing can have been written.
  expect(failures.map((entry) => entry.kind)).toEqual(Array(9).fill('refused'))
  expect(failures.every((entry) => entry.code === UNDO_UNLINK_NOT_AVAILABLE)).toBe(true)
  expect(mock.mock.calls.filter(([input]) => String(input).includes('/undo-unlink'))).toHaveLength(0)
})

test('a backend code that names an Object prototype member cannot render a function', () => {
  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
    expect(describeCaptureUndoUnlinkFailure(code)).toBe(CAPTURE_UNDO_UNLINK_FALLBACK_MESSAGE)
    const classified = classifyCaptureUndoUnlinkFailure(new ApiError(400, code, 'prose'))
    expect(classified.message).toBe(CAPTURE_UNDO_UNLINK_FALLBACK_MESSAGE)
    expect(typeof classified.message).toBe('string')
  }
})

test('an unrecognised thrown value is ambiguous rather than a claimed refusal', () => {
  expect(classifyCaptureUndoUnlinkFailure(undefined).kind).toBe('ambiguous')
  expect(classifyCaptureUndoUnlinkFailure(new Error('boom')).kind).toBe('ambiguous')
  expect(classifyCaptureUndoUnlinkFailure(new CaptureUndoUnlinkError(UNDO_UNLINK_NOT_AVAILABLE)).kind)
    .toBe('refused')
})
