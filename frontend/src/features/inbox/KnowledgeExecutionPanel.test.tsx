import { StrictMode } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { jsonResponse } from '../../test/fixtures'
import { ApiError, UnreadableSuccessError } from '../../api/transport'
import type { IssuedKnowledgeRequestWire } from '../../api/knowledge'
import { KnowledgeExecutionPanel } from './KnowledgeExecutionPanel'
import { knowledgeImportEnvelope, REQUEST_ID } from './knowledgeCaptureFixture'
import type { IssuedKnowledgeRequest } from './knowledgeRequestDraft'
import { KNOWLEDGE_REQUEST_SCHEMA } from './knowledgeRequestDraft'

const executeKnowledgeRequest = vi.fn()

vi.mock('../../api/knowledgeExecution', async () => {
  const actual = await vi.importActual<typeof import('../../api/knowledgeExecution')>(
    '../../api/knowledgeExecution',
  )
  return {
    ...actual,
    executeKnowledgeRequest: (...args: unknown[]) => executeKnowledgeRequest(...args),
  }
})

beforeEach(() => {
  executeKnowledgeRequest.mockReset()
})

const WORKSPACE = '22222222-2222-2222-2222-222222222222'
const OTHER_WORKSPACE = '33333333-3333-4333-8333-333333333333'
const OPEN_AT = Date.parse('2026-09-08T09:00:00.000Z')

function issuedRequest(overrides: Partial<IssuedKnowledgeRequest> = {}): IssuedKnowledgeRequest {
  return {
    schema: KNOWLEDGE_REQUEST_SCHEMA,
    request_id: REQUEST_ID,
    binding: { workspace_uid: WORKSPACE },
    purpose: 'find_context',
    query: 'rollback verification owner',
    corpus_refs: ['nas-team-share'],
    result_limit: 5,
    requested_at: '2026-09-08T09:00:00.000Z',
    expires_at: '2099-09-08T09:05:00.000Z',
    ...overrides,
  }
}

function renderPanel(overrides: {
  expired?: boolean
  now?: () => number
  onReview?: (envelope: unknown) => void
  request?: IssuedKnowledgeRequest
  strict?: boolean
  workspaceUid?: string
} = {}) {
  const onReview = overrides.onReview ?? vi.fn()
  const panel = (
    <KnowledgeExecutionPanel
      expired={overrides.expired ?? false}
      now={overrides.now ?? (() => OPEN_AT)}
      onReview={onReview}
      request={overrides.request ?? issuedRequest()}
      workspaceUid={overrides.workspaceUid ?? WORKSPACE}
    />
  )
  const view = render(overrides.strict ? <StrictMode>{panel}</StrictMode> : panel)
  return { onReview, ...view }
}

function runButton() {
  return screen.queryByRole('button', { name: 'Run connected search' })
}

function reviewButton() {
  return screen.queryByRole('button', { name: 'Review results' })
}

test('does not POST on mount and hides run when the receipt is already expired', () => {
  renderPanel({ expired: true })
  expect(runButton()).toBeNull()
  expect(executeKnowledgeRequest).not.toHaveBeenCalled()
  expect(reviewButton()).toBeNull()
})

test('rapid double click sends one POST', async () => {
  let release: (value: unknown) => void = () => {}
  executeKnowledgeRequest.mockImplementation(() => new Promise((resolve) => { release = resolve }))
  renderPanel()
  const button = screen.getByRole('button', { name: 'Run connected search' })
  fireEvent.click(button)
  fireEvent.click(button)
  expect(executeKnowledgeRequest).toHaveBeenCalledTimes(1)
  release(knowledgeImportEnvelope())
  await waitFor(() => expect(reviewButton()).not.toBeNull())
})

test('unknown, 403, malformed and wrong-request results do not retry or offer review', async () => {
  const cases: unknown[] = [
    new UnreadableSuccessError(200),
    new ApiError(403, 'forbidden', 'stale csrf leaked'),
    { hello: 'world' },
    knowledgeImportEnvelope({ request_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc' }),
  ]
  for (const result of cases) {
    executeKnowledgeRequest.mockReset()
    if (result instanceof Error) {
      executeKnowledgeRequest.mockRejectedValueOnce(result)
    } else {
      executeKnowledgeRequest.mockResolvedValueOnce(result)
    }
    const { unmount } = renderPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Run connected search' }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(reviewButton()).toBeNull()
    expect(runButton()).toBeNull()
    expect(executeKnowledgeRequest).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('alert').textContent).not.toContain('leaked')
    expect(screen.getByRole('alert').textContent).not.toMatch(/new request/i)
    unmount()
  }
})

test('a result that arrives after close is discarded', async () => {
  let release: (value: unknown) => void = () => {}
  executeKnowledgeRequest.mockImplementation(() => new Promise((resolve) => { release = resolve }))
  const { onReview, unmount } = renderPanel()
  fireEvent.click(screen.getByRole('button', { name: 'Run connected search' }))
  unmount()
  release(knowledgeImportEnvelope())
  await Promise.resolve()
  expect(onReview).not.toHaveBeenCalled()
})

test('a result for a previous workspace is discarded', async () => {
  let release: (value: unknown) => void = () => {}
  executeKnowledgeRequest.mockImplementation(() => new Promise((resolve) => { release = resolve }))
  const { onReview, rerender } = renderPanel()
  fireEvent.click(screen.getByRole('button', { name: 'Run connected search' }))
  rerender(
    <KnowledgeExecutionPanel
      expired={false}
      now={() => OPEN_AT}
      onReview={onReview}
      request={issuedRequest()}
      workspaceUid={OTHER_WORKSPACE}
    />,
  )
  release(knowledgeImportEnvelope())
  await Promise.resolve()
  await Promise.resolve()
  expect(reviewButton()).toBeNull()
  expect(onReview).not.toHaveBeenCalled()
})

test('expiry during work does not offer review as current', async () => {
  const clock = { ms: OPEN_AT }
  let release: (value: unknown) => void = () => {}
  executeKnowledgeRequest.mockImplementation(() => new Promise((resolve) => { release = resolve }))
  const { onReview } = renderPanel({
    now: () => clock.ms,
    request: issuedRequest({ expires_at: new Date(OPEN_AT + 1_000).toISOString() }),
  })
  fireEvent.click(screen.getByRole('button', { name: 'Run connected search' }))
  clock.ms = OPEN_AT + 5_000
  release(knowledgeImportEnvelope())
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/no longer current/i))
  expect(reviewButton()).toBeNull()
  expect(onReview).not.toHaveBeenCalled()
})

test('a valid result enables review without importing', async () => {
  const envelope = knowledgeImportEnvelope()
  executeKnowledgeRequest.mockResolvedValueOnce(envelope)
  const { onReview } = renderPanel()
  expect(executeKnowledgeRequest).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Run connected search' }))
  await waitFor(() => expect(reviewButton()).not.toBeNull())
  expect(onReview).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Review results' }))
  expect(onReview).toHaveBeenCalledTimes(1)
  expect(onReview).toHaveBeenCalledWith(expect.objectContaining({
    schema: 'workstack.knowledge-import.v1',
    request_id: REQUEST_ID,
  }))
})

test('StrictMode still settles a successful execute so review can be opened', async () => {
  executeKnowledgeRequest.mockResolvedValueOnce(knowledgeImportEnvelope())
  const { onReview } = renderPanel({ strict: true })
  fireEvent.click(screen.getByRole('button', { name: 'Run connected search' }))
  await waitFor(() => expect(reviewButton()).not.toBeNull())
  expect(executeKnowledgeRequest).toHaveBeenCalledTimes(1)
  expect(onReview).not.toHaveBeenCalled()
  expect(screen.queryByText('Waiting for the configured connector…')).toBeNull()
})

test('StrictMode still settles a failed execute without retry or review', async () => {
  executeKnowledgeRequest.mockRejectedValueOnce(
    new ApiError(503, 'knowledge_driver_unavailable', 'driver log must not leak'),
  )
  renderPanel({ strict: true })
  fireEvent.click(screen.getByRole('button', { name: 'Run connected search' }))
  await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
  expect(reviewButton()).toBeNull()
  expect(runButton()).toBeNull()
  expect(executeKnowledgeRequest).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('alert').textContent).toMatch(/no search connector is configured/i)
  expect(screen.getByRole('alert').textContent).not.toContain('leak')
})

test('a constructor HTTP error code shows the fallback and does not retry', async () => {
  const actual = await vi.importActual<typeof import('../../api/knowledgeExecution')>(
    '../../api/knowledgeExecution',
  )
  executeKnowledgeRequest.mockImplementation((request: unknown) => (
    actual.executeKnowledgeRequest(request as IssuedKnowledgeRequestWire)
  ))
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    if (url === actual.KNOWLEDGE_EXECUTE_PATH) {
      return jsonResponse({
        error: { code: 'constructor', message: 'raw backend detail: C:\\srv\\nas' },
      }, 500)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)

  renderPanel()
  fireEvent.click(screen.getByRole('button', { name: 'Run connected search' }))
  const alert = await screen.findByRole('alert')
  expect(alert.textContent).toBe(
    'The search outcome is not known. Check the connector, or paste an existing result if you have one. This request is not sent again.',
  )
  expect(alert.textContent).not.toMatch(/native code|\[object Object\]|function /)
  expect(alert.textContent).not.toContain('nas')
  expect(screen.getByRole('region', { name: 'Connected search' })).toBeInTheDocument()
  expect(reviewButton()).toBeNull()
  expect(runButton()).toBeNull()
  expect(executeKnowledgeRequest).toHaveBeenCalledTimes(1)
  const posts = fetchMock.mock.calls.filter(([input, init]) => (
    String(input) === actual.KNOWLEDGE_EXECUTE_PATH && init?.method === 'POST'
  ))
  expect(posts).toHaveLength(1)
})
