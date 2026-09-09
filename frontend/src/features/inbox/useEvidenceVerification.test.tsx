import { StrictMode } from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { capture, jsonResponse, workspace } from '../../test/fixtures'
import { KNOWLEDGE_CAPTURE_VERIFY_PATH } from '../../api/knowledgeVerification'
import type { Capture, CaptureV11 } from '../../domain/types'
import { knowledgeCapture, retrievalProjection } from './knowledgeCaptureFixture'
import { useEvidenceVerification } from './useEvidenceVerification'

const WORKSPACE_A = workspace.workspace.id
const WORKSPACE_B = '33333333-3333-4333-8333-333333333333'
const DOC = 'od-page-7f3ba1d34f50c884600112ab'

function resultEnvelope(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      binding: { workspace_uid: WORKSPACE_A, capture_id: 'C-0001', capture_revision: 0 },
      result: {
        schema: 'workstack.knowledge-verification.v1',
        verification_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
        checked_at: '2026-09-09T10:15:02Z',
        evidence: [{
          document_ref: DOC,
          source_type: 'notion.page',
          expected_source_version: 'od-version-14',
          observed_source_version: 'od-version-14',
          status: 'current',
          code: 'hash_matched',
        }],
        ...(overrides.result as Record<string, unknown> | undefined),
      },
      ...(overrides.data as Record<string, unknown> | undefined),
    },
    meta: { outcome: 'verification_ready' },
  }
}

interface VerifyExchange {
  calls: () => number
  settle: (envelope: unknown, status?: number) => Promise<void>
}

/** One deferred POST per click, so a late answer can be delivered on purpose. */
function stubVerifyExchange(): VerifyExchange {
  const waiting: Array<(response: Response) => void> = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    return new Promise<Response>((resolve) => waiting.push(resolve))
  }))
  return {
    calls: () => (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
      .filter(([input]: unknown[]) => String(input) === KNOWLEDGE_CAPTURE_VERIFY_PATH).length,
    settle: async (envelope, status = 200) => {
      // The CSRF preflight resolves first, so the POST resolver appears a tick later.
      await waitFor(() => { expect(waiting.length).toBeGreaterThan(0) })
      const resolve = waiting.shift()
      await act(async () => {
        resolve?.(new Response(JSON.stringify(envelope), {
          status,
          headers: { 'Content-Type': 'application/json' },
        }))
        await new Promise((done) => { setTimeout(done, 0) })
      })
    },
  }
}

function renderVerification(initial: { capture: Capture; workspaceUid?: string }, strict = false) {
  return renderHook(
    (props: { capture: Capture; workspaceUid?: string }) =>
      useEvidenceVerification(props.capture, props.workspaceUid),
    { initialProps: initial, wrapper: strict ? StrictMode : undefined },
  )
}

function revised(revision: number): CaptureV11 {
  return knowledgeCapture({ revision })
}

afterEach(() => { vi.unstubAllGlobals() })

test('nothing is requested on render, on a rerender or on a reopen — only on a click', async () => {
  const exchange = stubVerifyExchange()
  const view = renderVerification({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  view.rerender({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  view.unmount()
  renderVerification({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await waitFor(() => expect(exchange.calls()).toBe(0))
})

test('a check is offered only for a 1.1 capture with an explicit canonical workspace UID', () => {
  const withEvidence = knowledgeCapture()
  expect(renderVerification({ capture: withEvidence, workspaceUid: WORKSPACE_A }).result.current.available).toBe(true)
  expect(renderVerification({ capture: withEvidence }).result.current.available).toBe(false)
  expect(renderVerification({ capture: withEvidence, workspaceUid: 'W-1' }).result.current.available).toBe(false)
  // Old Microsoft 1.0 records carry no retrieval evidence at all.
  expect(renderVerification({ capture, workspaceUid: WORKSPACE_A }).result.current.available).toBe(false)
  expect(renderVerification({
    capture: knowledgeCapture({
      retrieval: retrievalProjection({
        answer_scope: 'synthesized',
        origin_state: 'synthesized',
        reported_origin: null,
        evidence: Array.from({ length: 11 }, () => retrievalProjection().evidence[0]),
      }),
    }),
    workspaceUid: WORKSPACE_A,
  }).result.current.available).toBe(false)
})

test('the lock is taken synchronously, so a double click is one POST', async () => {
  const exchange = stubVerifyExchange()
  const view = renderVerification({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  act(() => {
    view.result.current.check()
    view.result.current.check()
  })
  expect(view.result.current.pending).toBe(true)
  await waitFor(() => expect(exchange.calls()).toBe(1))
  await exchange.settle(resultEnvelope())
  await waitFor(() => expect(view.result.current.observation?.checkedAt).toBe('2026-09-09T10:15:02Z'))
  expect(exchange.calls()).toBe(1)
})

test('a revision change clears the observation in the same commit and drops the late answer', async () => {
  const exchange = stubVerifyExchange()
  const view = renderVerification({ capture: revised(0), workspaceUid: WORKSPACE_A })
  act(() => view.result.current.check())
  view.rerender({ capture: revised(1), workspaceUid: WORKSPACE_A })
  // The new revision is unlocked at once: the previous answer can no longer land on it.
  expect(view.result.current.pending).toBe(false)
  expect(view.result.current.observation).toBeNull()
  await exchange.settle(resultEnvelope())
  expect(view.result.current.observation).toBeNull()
  expect(view.result.current.error).toBeNull()
})

test('an A→B→A workspace swap does not let the first A answer land on the second A', async () => {
  const exchange = stubVerifyExchange()
  const subject = knowledgeCapture()
  const view = renderVerification({ capture: subject, workspaceUid: WORKSPACE_A })
  act(() => view.result.current.check())
  view.rerender({ capture: subject, workspaceUid: WORKSPACE_B })
  view.rerender({ capture: subject, workspaceUid: WORKSPACE_A })
  await exchange.settle(resultEnvelope())
  expect(view.result.current.observation).toBeNull()
  expect(view.result.current.pending).toBe(false)
})

test('a close and reopen starts clean, and the closed view never adopts its own late answer', async () => {
  const exchange = stubVerifyExchange()
  const first = renderVerification({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  act(() => first.result.current.check())
  first.unmount()
  await exchange.settle(resultEnvelope())
  const second = renderVerification({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  expect(second.result.current.observation).toBeNull()
  expect(second.result.current.pending).toBe(false)
  expect(exchange.calls()).toBe(1)
})

test('under StrictMode the double mount still checks once and shows the answer', async () => {
  const exchange = stubVerifyExchange()
  const view = renderVerification({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A }, true)
  await waitFor(() => expect(exchange.calls()).toBe(0))
  act(() => view.result.current.check())
  await waitFor(() => expect(exchange.calls()).toBe(1))
  await exchange.settle(resultEnvelope())
  await waitFor(() => expect(view.result.current.observation?.entries).toHaveLength(1))
  expect(view.result.current.observation?.entries[0].status).toBe('current')
})

test('a failed retry clears the previous observation and reports closed copy only', async () => {
  const exchange = stubVerifyExchange()
  const view = renderVerification({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  act(() => view.result.current.check())
  await exchange.settle(resultEnvelope())
  await waitFor(() => expect(view.result.current.observation).not.toBeNull())

  act(() => view.result.current.check())
  expect(view.result.current.observation).toBeNull()
  await exchange.settle({ error: { code: 'verification_busy', message: 'pid 4711 holds /srv/nas' } }, 409)
  await waitFor(() => expect(view.result.current.error)
    .toBe('Another source check is already running. Try again once it finishes.'))
  expect(view.result.current.error).not.toContain('nas')
  expect(view.result.current.observation).toBeNull()
  expect(view.result.current.pending).toBe(false)
})

test('an answer bound to another capture is refused rather than displayed', async () => {
  const exchange = stubVerifyExchange()
  const view = renderVerification({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  act(() => view.result.current.check())
  await exchange.settle(resultEnvelope({ data: {
    binding: { workspace_uid: WORKSPACE_A, capture_id: 'C-0002', capture_revision: 0 },
  } }))
  await waitFor(() => expect(view.result.current.error)
    .toBe('The answer did not match this capture, so nothing was accepted.'))
  expect(view.result.current.observation).toBeNull()
})

test('a prototype-named backend code and a non-200 success both render bounded copy', async () => {
  const exchange = stubVerifyExchange()
  const view = renderVerification({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })

  // `constructor` resolves through `Object.prototype` on an ordinary lookup table, which
  // would put a function — not a sentence — into the view state the panel renders.
  act(() => view.result.current.check())
  await exchange.settle({ error: { code: 'constructor', message: 'raw backend detail: /srv/nas' } }, 500)
  await waitFor(() => expect(view.result.current.error).toBe('The source check could not be completed.'))
  expect(typeof view.result.current.error).toBe('string')
  expect(view.result.current.error).not.toContain('nas')
  expect(view.result.current.observation).toBeNull()

  // A well-formed body returned as 201 is not the one success status R21 publishes.
  act(() => view.result.current.check())
  await exchange.settle(resultEnvelope(), 201)
  await waitFor(() => expect(view.result.current.error)
    .toBe('The source check answer could not be read, so nothing was checked.'))
  expect(view.result.current.observation).toBeNull()
  expect(exchange.calls()).toBe(2)
})
