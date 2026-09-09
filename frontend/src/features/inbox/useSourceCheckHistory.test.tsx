import { StrictMode } from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { capture, jsonResponse, workspace } from '../../test/fixtures'
import {
  KNOWLEDGE_CAPTURE_OBSERVATION_PATH,
  KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH,
} from '../../api/knowledgeObservation'
import { KNOWLEDGE_CAPTURE_VERIFY_PATH } from '../../api/knowledgeVerification'
import type { Capture, CaptureV11 } from '../../domain/types'
import { knowledgeCapture } from './knowledgeCaptureFixture'
import { useSourceCheckHistory } from './useSourceCheckHistory'

const WORKSPACE_A = workspace.workspace.id
const WORKSPACE_B = '33333333-3333-4333-8333-333333333333'
const DOC = 'od-page-7f3ba1d34f50c884600112ab'

function evidence() {
  return [{
    document_ref: DOC,
    source_type: 'notion.page',
    expected_source_version: 'od-version-14',
    observed_source_version: 'od-version-14',
    status: 'current',
    code: 'hash_matched',
  }]
}

function unchanged(checkedAt = '2026-09-08T10:15:02Z') {
  return {
    accepted_at: '2026-09-08T10:15:04Z',
    checked_at: checkedAt,
    binding_state: 'unchanged',
    result: {
      schema: 'workstack.knowledge-verification.v1',
      verification_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
      checked_at: checkedAt,
      evidence: evidence(),
    },
  }
}

const CHANGED = {
  accepted_at: '2026-09-08T10:15:04Z',
  checked_at: '2026-09-08T10:15:02Z',
  binding_state: 'changed',
  result: null,
}

function envelope(observation: unknown, outcome: string, revision = 0) {
  return {
    data: {
      binding: { workspace_uid: WORKSPACE_A, capture_id: 'C-0001', capture_revision: revision },
      observation,
    },
    meta: { outcome },
  }
}

const ready = (observation: unknown, revision = 0) => envelope(observation, 'observation_ready', revision)
const recorded = (observation: unknown) => envelope(observation, 'observation_recorded')

type Responder = (target: string) => Promise<Response>

/** Every route answered from one place, so an unexpected call is visible as a call. */
function stubRoutes(respond: Responder) {
  const mock = vi.fn((input: RequestInfo | URL) => {
    const target = String(input)
    if (target.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    return respond(target)
  })
  vi.stubGlobal('fetch', mock)
  return {
    mock,
    calls: (path: string) => mock.mock.calls.filter(([input]) => String(input).startsWith(path)).length,
  }
}

/** One deferred answer per request, so a late one can be delivered on purpose. */
function deferredRoutes() {
  const waiting = new Map<string, Array<(response: Response) => void>>()
  const routes = stubRoutes(async (target) => await new Promise<Response>((resolve) => {
    const key = target.split('?')[0]
    waiting.set(key, [...(waiting.get(key) ?? []), resolve])
  }))
  return {
    ...routes,
    settle: async (path: string, body: unknown, status = 200, index = 0) => {
      await waitFor(() => { expect(waiting.get(path)?.length ?? 0).toBeGreaterThan(index) })
      const [resolve] = waiting.get(path)?.splice(index, 1) ?? []
      await act(async () => {
        resolve?.(new Response(JSON.stringify(body), {
          status,
          headers: { 'Content-Type': 'application/json' },
        }))
        await new Promise((done) => { setTimeout(done, 0) })
      })
    },
  }
}

function renderHistory(initial: { capture: Capture; workspaceUid?: string }, strict = false) {
  return renderHook(
    (props: { capture: Capture; workspaceUid?: string }) =>
      useSourceCheckHistory(props.capture, props.workspaceUid),
    { initialProps: initial, wrapper: strict ? StrictMode : undefined },
  )
}

function revised(revision: number): CaptureV11 {
  return knowledgeCapture({ revision })
}

afterEach(() => { vi.unstubAllGlobals() })

test('opening a capture reads the saved check once and never posts', async () => {
  const routes = stubRoutes(async () => await jsonResponse(ready(unchanged())))
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })

  await waitFor(() => expect(view.result.current.mode).toBe('supported'))
  expect(view.result.current.saved?.bindingState).toBe('unchanged')
  expect(view.result.current.saved?.checkedAt).toBe('2026-09-08T10:15:02Z')
  expect(view.result.current.saved?.entries).toHaveLength(1)
  expect(routes.calls(KNOWLEDGE_CAPTURE_OBSERVATION_PATH)).toBe(1)
  // The read is the only automatic request. Nothing about mounting records a check.
  expect(routes.calls(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toBe(0)
  expect(routes.calls(KNOWLEDGE_CAPTURE_VERIFY_PATH)).toBe(0)

  // A rerender of the same capture is not a new view, so it does not re-read.
  view.rerender({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await waitFor(() => expect(routes.calls(KNOWLEDGE_CAPTURE_OBSERVATION_PATH)).toBe(1))
})

test('a capture with no checkable identity reads nothing at all', async () => {
  const routes = stubRoutes(async () => await jsonResponse(ready(null)))

  expect(renderHistory({ capture: knowledgeCapture() }).result.current.mode).toBe('disabled')
  expect(renderHistory({ capture: knowledgeCapture(), workspaceUid: 'W-1' }).result.current.mode).toBe('disabled')
  // A 1.0 record carries no retrieval evidence, so there is nothing to check or overlay.
  expect(renderHistory({ capture, workspaceUid: WORKSPACE_A }).result.current.mode).toBe('disabled')
  await waitFor(() => expect(routes.mock).not.toHaveBeenCalled())
})

test('an empty history says so without claiming anything was or was not written', async () => {
  stubRoutes(async () => await jsonResponse(ready(null)))
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await waitFor(() => expect(view.result.current.mode).toBe('supported'))
  expect(view.result.current.saved).toBeNull()
  expect(view.result.current.error).toBeNull()
})

test('a changed binding keeps the time and drops the evidence', async () => {
  stubRoutes(async () => await jsonResponse(ready(CHANGED)))
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await waitFor(() => expect(view.result.current.saved?.bindingState).toBe('changed'))
  expect(view.result.current.saved?.checkedAt).toBe('2026-09-08T10:15:02Z')
  expect(view.result.current.saved?.entries).toBeNull()
})

test('saved evidence that does not match the rendered rows leaves the surface undetermined', async () => {
  stubRoutes(async () => await jsonResponse(ready({
    ...unchanged(),
    result: {
      ...unchanged().result,
      evidence: [{ ...evidence()[0], document_ref: 'od-page-0000000000000000000000ff' }],
    },
  })))
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await waitFor(() => expect(view.result.current.mode).toBe('unavailable'))
  expect(view.result.current.saved).toBeNull()
  expect(view.result.current.error).toBe('Saved source checks could not be loaded from this server.')
})

test('exactly a not_found 404 selects the old readonly surface, and other failures do not', async () => {
  stubRoutes(async () => await jsonResponse({ error: { code: 'not_found', message: 'no route' } }, 404))
  const legacy = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await waitFor(() => expect(legacy.result.current.mode).toBe('legacy'))
  expect(legacy.result.current.error).toBeNull()

  for (const [body, status] of [
    [{ error: { code: 'unknown_capture', message: 'gone' } }, 404],
    [{ error: { code: 'observation_read_unavailable', message: '/srv/store.db' } }, 503],
    [ready(null), 206],
  ] as const) {
    vi.unstubAllGlobals()
    stubRoutes(async () => await jsonResponse(body, status))
    const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
    await waitFor(() => expect(view.result.current.mode).toBe('unavailable'))
    expect(view.result.current.error).toBe('Saved source checks could not be loaded from this server.')
  }
})

test('a network failure is undetermined, not an old-server diagnosis', async () => {
  stubRoutes(async () => { throw new TypeError('Failed to fetch') })
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await waitFor(() => expect(view.result.current.mode).toBe('unavailable'))
  expect(view.result.current.mode).not.toBe('legacy')
})

test('the record lock is taken synchronously, so a double click is one POST', async () => {
  const routes = deferredRoutes()
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(null))
  await waitFor(() => expect(view.result.current.mode).toBe('supported'))

  act(() => {
    view.result.current.recordCheck()
    view.result.current.recordCheck()
  })
  expect(view.result.current.pending).toBe(true)
  await routes.settle(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH, recorded(unchanged()))
  await waitFor(() => expect(view.result.current.saved?.entries).toHaveLength(1))
  expect(routes.calls(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toBe(1)
  expect(view.result.current.needsReload).toBe(false)
})

test('a failed record clears the rows, keeps closed copy and waits for an explicit reload', async () => {
  const routes = deferredRoutes()
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(unchanged()))
  await waitFor(() => expect(view.result.current.saved).not.toBeNull())

  act(() => view.result.current.recordCheck())
  expect(view.result.current.saved).toBeNull()
  await routes.settle(
    KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH,
    { error: { code: 'observation_save_unknown', message: 'OSError 28 on /srv/store.db' } },
    503,
  )
  await waitFor(() => expect(view.result.current.needsReload).toBe(true))
  expect(view.result.current.error).toContain('could not be confirmed')
  expect(view.result.current.error).not.toContain('OSError')
  expect(view.result.current.saved).toBeNull()

  // No automatic repeat, and no fallback to the old readonly route.
  expect(routes.calls(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toBe(1)
  expect(routes.calls(KNOWLEDGE_CAPTURE_VERIFY_PATH)).toBe(0)

  // The record action stays shut until the reader reloads.
  act(() => view.result.current.recordCheck())
  expect(routes.calls(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toBe(1)

  act(() => view.result.current.reload())
  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(unchanged('2026-09-08T11:00:00Z')))
  await waitFor(() => expect(view.result.current.needsReload).toBe(false))
  expect(view.result.current.error).toBeNull()
  expect(view.result.current.saved?.checkedAt).toBe('2026-09-08T11:00:00Z')

  // And only now is another explicit check possible — a new check, not a hidden retry.
  act(() => view.result.current.recordCheck())
  await waitFor(() => expect(routes.calls(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toBe(2))
})

test('a late read never overwrites the newer recorded check for the same view', async () => {
  const routes = deferredRoutes()
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(null))
  await waitFor(() => expect(view.result.current.mode).toBe('supported'))

  // Two reload clicks inside one tick are two reads: React has not re-rendered between
  // them, so both see an idle surface. This is how a stale read gets to exist at all.
  act(() => {
    view.result.current.reload()
    view.result.current.reload()
  })
  await waitFor(() => expect(routes.calls(KNOWLEDGE_CAPTURE_OBSERVATION_PATH)).toBe(3))

  // The newer of the two answers first and settles the surface.
  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(unchanged('2026-09-08T10:00:00Z')), 200, 1)
  await waitFor(() => expect(view.result.current.saved?.checkedAt).toBe('2026-09-08T10:00:00Z'))

  act(() => view.result.current.recordCheck())
  await routes.settle(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH, recorded(unchanged('2026-09-08T12:00:00Z')))
  await waitFor(() => expect(view.result.current.saved?.checkedAt).toBe('2026-09-08T12:00:00Z'))

  // The first read finally lands, carrying an older saved check. It is a belief about a
  // moment this view has already moved past, so it is dropped rather than displayed.
  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(unchanged('2026-09-08T09:00:00Z')))
  expect(view.result.current.saved?.checkedAt).toBe('2026-09-08T12:00:00Z')
  expect(view.result.current.mode).toBe('supported')
})

test('a read that is still open keeps the record action shut', async () => {
  const routes = deferredRoutes()
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })

  // The history read has not answered yet: a check cannot be recorded over an unknown.
  act(() => view.result.current.recordCheck())
  expect(routes.calls(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toBe(0)
  expect(view.result.current.mode).toBe('loading')

  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(null))
  await waitFor(() => expect(view.result.current.mode).toBe('supported'))
  act(() => view.result.current.recordCheck())
  await waitFor(() => expect(routes.calls(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toBe(1))
})

test('a revision change clears the saved check in the same commit and re-reads for the new view', async () => {
  const routes = deferredRoutes()
  const view = renderHistory({ capture: revised(0), workspaceUid: WORKSPACE_A })
  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(unchanged()))
  await waitFor(() => expect(view.result.current.saved).not.toBeNull())

  act(() => view.result.current.recordCheck())
  view.rerender({ capture: revised(1), workspaceUid: WORKSPACE_A })
  expect(view.result.current.saved).toBeNull()
  expect(view.result.current.pending).toBe(false)

  // The previous revision's recorded answer can no longer land on the new one.
  await routes.settle(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH, recorded(unchanged()))
  expect(view.result.current.saved).toBeNull()
  expect(view.result.current.error).toBeNull()
  await waitFor(() => expect(routes.calls(KNOWLEDGE_CAPTURE_OBSERVATION_PATH)).toBe(2))
})

test('an A→B→A workspace swap does not let the first A answer land on the second A', async () => {
  const routes = deferredRoutes()
  const subject = knowledgeCapture()
  const view = renderHistory({ capture: subject, workspaceUid: WORKSPACE_A })
  view.rerender({ capture: subject, workspaceUid: WORKSPACE_B })
  view.rerender({ capture: subject, workspaceUid: WORKSPACE_A })

  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(unchanged()))
  expect(view.result.current.saved).toBeNull()
  expect(view.result.current.mode).toBe('loading')
})

test('a closed view never adopts its own late answer, and reopening starts clean', async () => {
  const routes = deferredRoutes()
  const first = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  first.unmount()
  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(unchanged()))

  const second = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A })
  expect(second.result.current.saved).toBeNull()
  await routes.settle(KNOWLEDGE_CAPTURE_OBSERVATION_PATH, ready(null))
  await waitFor(() => expect(second.result.current.mode).toBe('supported'))
  expect(second.result.current.saved).toBeNull()
})

test('under StrictMode the duplicate read is harmless and still records exactly once', async () => {
  const routes = stubRoutes(async (target) => await jsonResponse(
    target.startsWith(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH) ? recorded(unchanged()) : ready(null),
  ))
  const view = renderHistory({ capture: knowledgeCapture(), workspaceUid: WORKSPACE_A }, true)
  await waitFor(() => expect(view.result.current.mode).toBe('supported'))
  expect(routes.calls(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toBe(0)

  act(() => view.result.current.recordCheck())
  await waitFor(() => expect(view.result.current.saved?.entries).toHaveLength(1))
  expect(routes.calls(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)).toBe(1)
})
