import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { capture, jsonResponse, task, workspace } from '../../test/fixtures'
import { verifiedMicrosoftProviderGates } from '../../test/providerGates'
import type { Capture } from '../../domain/types'
import { CaptureDrawer } from './CaptureDrawer'
import { knowledgeCapture } from './knowledgeCaptureFixture'

test('capture calendar keeps date-only intent, clears to null and disables during submission', async () => {
  let finish!: () => void
  const onCreateTask = vi.fn(() => new Promise<typeof task>((resolve) => { finish = () => resolve(task) }))
  render(<CaptureDrawer capture={capture} onClose={vi.fn()} onCreateTask={onCreateTask} workspace={workspace} />)
  await userEvent.click(screen.getByRole('button', { name: 'Create task from this source' }))
  const due = screen.getByLabelText('Due')
  fireEvent.change(due, { target: { value: '2024-02-28' } })
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  await userEvent.click(screen.getByRole('button', { name: 'February 29, 2024' }))
  expect(onCreateTask).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Create linked task' }))
  expect(onCreateTask).toHaveBeenLastCalledWith(expect.objectContaining({ due: '2024-02-29' }))
  expect(due).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Choose date' })).toBeDisabled()
  await act(async () => finish())
  await userEvent.click(within(due.closest('.date-input-control')! as HTMLElement).getByRole('button', { name: 'Clear date' }))
  await userEvent.click(screen.getByRole('button', { name: 'Create linked task' }))
  expect(onCreateTask).toHaveBeenLastCalledWith(expect.objectContaining({ due: null }))
  await act(async () => finish())
})

test('creates a generic task with source title and sanitized context prefilled', async () => {
  const onCreateTask = vi.fn().mockResolvedValue(task)
  render(
    <CaptureDrawer
      capture={capture}
      onClose={vi.fn()}
      onCreateTask={onCreateTask}
      workspace={workspace}
    />,
  )

  await userEvent.click(screen.getByRole('button', { name: 'Create task from this source' }))
  expect(screen.getByLabelText('Task title')).toHaveValue(capture.source.display_title)
  expect(screen.getByLabelText('Definition of done / source context')).toHaveValue(capture.normalized.context)
  await userEvent.click(screen.getByRole('button', { name: 'Create linked task' }))

  expect(onCreateTask).toHaveBeenCalledWith({
    title: capture.source.display_title,
    detail: capture.normalized.context,
    priority: 'P2',
    due: null,
    tags: capture.normalized.tags,
    objective_ids: [],
  })
})

test('never calls supplied OOB provenance verified until that provider Gate 0 passes', () => {
  const oobCapture: Capture = {
    ...capture,
    provenance: {
      capture_mode: 'oob_verified',
      adapter: 'outlook-agent',
      adapter_version: '1.0.0',
      model: 'agent-model',
      prompt_version: 'capture-v1',
      redaction_policy_version: 'workstack-redaction-v1',
      tool_trace_digest: `sha256:${'c'.repeat(64)}`,
      allowed_tools: ['microsoft.outlook.read', 'workstack.capture.write'],
      raw_retained: false,
      created_at: capture.created_at,
    },
  }
  const props = { onClose: vi.fn(), onCreateTask: vi.fn().mockResolvedValue(task), workspace }
  const { rerender } = render(<CaptureDrawer capture={oobCapture} {...props} />)

  expect(screen.getByText('Supplied provenance · Gate 0 unverified')).toBeInTheDocument()
  expect(screen.queryByText('OOB verified')).not.toBeInTheDocument()

  rerender(<CaptureDrawer capture={oobCapture} providerGates={verifiedMicrosoftProviderGates} {...props} />)
  expect(screen.getByText('OOB verified')).toBeInTheDocument()
})

test('rebases a pristine source draft when the same Capture receives a newer revision', async () => {
  const props = { onClose: vi.fn(), onCreateTask: vi.fn().mockResolvedValue(task), workspace }
  const { rerender } = render(<CaptureDrawer capture={capture} {...props} />)

  await userEvent.click(screen.getByRole('button', { name: 'Create task from this source' }))
  rerender(<CaptureDrawer capture={{
    ...capture,
    revision: 1,
    source: { ...capture.source, display_title: 'Updated release feedback', version_ref: 'change-key:v2', fingerprint: `sha256:${'c'.repeat(64)}` },
    normalized: { ...capture.normalized, context: 'Updated sanitized context.', tags: ['release', 'follow-up'] },
  }} {...props} />)

  expect(screen.getByLabelText('Task title')).toHaveValue('Updated release feedback')
  expect(screen.getByLabelText('Definition of done / source context')).toHaveValue('Updated sanitized context.')
  expect(screen.getByLabelText('Tags comma separated')).toHaveValue('release, follow-up')
  expect(screen.getByText('Source draft refreshed to Capture revision 1.')).toBeVisible()
})

test('preserves dirty source fields until the user resolves a newer Capture revision', async () => {
  const user = userEvent.setup()
  const props = { onClose: vi.fn(), onCreateTask: vi.fn().mockResolvedValue(task), workspace }
  const { rerender } = render(<CaptureDrawer capture={capture} {...props} />)

  await user.click(screen.getByRole('button', { name: 'Create task from this source' }))
  const title = screen.getByLabelText('Task title')
  await user.clear(title)
  await user.type(title, 'My reviewed task title')
  await user.selectOptions(screen.getByLabelText('Priority'), 'P0')

  rerender(<CaptureDrawer capture={{ ...capture, revision: 1, status: 'linked' }} {...props} />)
  expect(title).toHaveValue('My reviewed task title')
  expect(screen.queryByRole('status', { name: 'Capture source updated' })).not.toBeInTheDocument()

  rerender(<CaptureDrawer capture={{
    ...capture,
    revision: 2,
    source: { ...capture.source, display_title: 'New server title', version_ref: 'change-key:v3', fingerprint: `sha256:${'d'.repeat(64)}` },
    normalized: { ...capture.normalized, context: 'New server context.', tags: ['new'] },
  }} {...props} />)

  expect(title).toHaveValue('My reviewed task title')
  expect(screen.getByRole('status', { name: 'Capture source updated' })).toHaveTextContent('revision 1 to 2')
  await user.click(screen.getByRole('button', { name: 'Refresh source fields' }))
  expect(title).toHaveValue('New server title')
  expect(screen.getByLabelText('Definition of done / source context')).toHaveValue('New server context.')
  expect(screen.getByLabelText('Tags comma separated')).toHaveValue('new')
  expect(screen.getByLabelText('Priority')).toHaveValue('P0')
  expect(screen.queryByRole('status', { name: 'Capture source updated' })).not.toBeInTheDocument()
})

test('1.1 knowledge captures show evidence without source-open or automatic Task creation', async () => {
  const onCreateTask = vi.fn().mockResolvedValue(task)
  const listed = knowledgeCapture()
  render(<CaptureDrawer capture={listed} onClose={vi.fn()} onCreateTask={onCreateTask} workspace={workspace} />)

  expect(screen.getByText('Manual import / unverified source')).toBeInTheDocument()
  expect(screen.getByText('Reported Notion page (claim)')).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: /open microsoft source/i })).not.toBeInTheDocument()
  expect(onCreateTask).not.toHaveBeenCalled()

  await userEvent.click(screen.getByRole('button', { name: 'Create task from this source' }))
  await userEvent.click(screen.getByRole('button', { name: 'Create linked task' }))
  expect(onCreateTask).toHaveBeenCalledWith({
    title: listed.source.display_title,
    detail: listed.normalized.context,
    priority: 'P2',
    due: null,
    tags: listed.normalized.tags,
    objective_ids: [],
  })
})

afterEach(() => { vi.unstubAllGlobals() })

test('the drawer hands its own workspace UID to the evidence panel and the verify route', async () => {
  const listed = knowledgeCapture()
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    // R27-E: the panel reads the saved source check when the drawer opens. This owner
    // answers with a plain not_found 404 — the one old-server signal — so the drawer
    // keeps R21's transient check, which is what the rest of this test is about.
    if (String(input).startsWith('/api/v1/knowledge/captures/observation')) {
      return jsonResponse({ error: { code: 'not_found', message: 'no route' } }, 404)
    }
    return jsonResponse({
      data: {
        binding: { workspace_uid: workspace.workspace.id, capture_id: listed.id, capture_revision: listed.revision },
        result: {
          schema: 'workstack.knowledge-verification.v1',
          verification_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
          checked_at: '2026-09-09T10:15:02Z',
          evidence: [{
            document_ref: listed.retrieval.evidence[0].document_ref,
            source_type: listed.retrieval.evidence[0].reported_source_type,
            expected_source_version: listed.retrieval.evidence[0].reported_source_version,
            observed_source_version: null,
            status: 'unavailable',
            code: 'root_unavailable',
          }],
        },
      },
      meta: { outcome: 'verification_ready' },
    })
  })
  vi.stubGlobal('fetch', mock)

  render(<CaptureDrawer capture={listed} onClose={vi.fn()} onCreateTask={vi.fn()} workspace={workspace} />)
  // The saved-check read is the only request an open drawer makes on its own.
  await screen.findByText('Saved checks are unavailable on this server.')
  expect(mock.mock.calls.filter(([input]) => String(input) === '/api/v1/knowledge/captures/verify')).toHaveLength(0)

  await userEvent.click(screen.getByRole('button', { name: 'Check source status' }))
  expect(await screen.findByText('Source unavailable')).toBeInTheDocument()

  const post = mock.mock.calls.find(([input]) => String(input) === '/api/v1/knowledge/captures/verify')
  expect(JSON.parse(String(post?.[1]?.body))).toEqual({
    workspace_uid: workspace.workspace.id,
    capture_id: listed.id,
    capture_revision: listed.revision,
  })
  // The observation is transient: the stored claims are still the only saved trust state.
  expect(screen.getByText('Manual import / unverified source')).toBeInTheDocument()
  expect(screen.getByText('Reported Notion page (claim)')).toBeInTheDocument()
})

test('a 1.0 Microsoft capture in the same drawer offers no source check at all', () => {
  const mock = vi.fn(() => jsonResponse({}))
  vi.stubGlobal('fetch', mock)
  render(<CaptureDrawer capture={capture} onClose={vi.fn()} onCreateTask={vi.fn()} workspace={workspace} />)
  expect(screen.queryByRole('button', { name: 'Check source status' })).not.toBeInTheDocument()
  expect(mock).not.toHaveBeenCalled()
})

/**
 * R31-A: the entry that starts a new reviewed search from this saved source.
 *
 * The drawer owns two decisions here and nothing else — whether to offer the action at
 * all, and what the sentence above it says. It starts no search of its own, and the
 * Capture on screen is neither re-read nor changed by anything below.
 */

test('the updated-context entry is hidden entirely when the parent has no review handoff', () => {
  const mock = vi.fn(() => jsonResponse({}))
  vi.stubGlobal('fetch', mock)
  render(<CaptureDrawer capture={capture} onClose={vi.fn()} onCreateTask={vi.fn()} workspace={workspace} />)

  // A launcher whose result nobody can review is a dead end, so it is not rendered.
  expect(screen.queryByRole('button', { name: 'Search for updated context' })).not.toBeInTheDocument()
  expect(screen.queryByText(/Start a new search for your review/)).not.toBeInTheDocument()
  expect(mock).not.toHaveBeenCalled()
})

test('with a review handoff the entry appears, says what it does, and reads nothing on mount', () => {
  const mock = vi.fn(() => jsonResponse({}))
  vi.stubGlobal('fetch', mock)
  render(
    <CaptureDrawer
      capture={capture}
      onClose={vi.fn()}
      onCreateTask={vi.fn()}
      onReviewKnowledge={vi.fn()}
      workspace={workspace}
    />,
  )

  expect(screen.getByRole('button', { name: 'Search for updated context' })).toBeInTheDocument()
  // The sentence is the honest one: a new search, and a capture left alone. It does not
  // promise the old document is re-read, and it does not claim a refresh happened.
  expect(
    screen.getByText('Start a new search for your review. This capture stays unchanged.'),
  ).toBeInTheDocument()
  expect(document.body.textContent).not.toMatch(/refreshed|up to date|now current/i)
  // Opening a drawer is not a search: no policy read, no request, no provider call.
  expect(mock).not.toHaveBeenCalled()
})

test('a 1.1 capture still makes only the saved-check read when the drawer opens', async () => {
  const listed = knowledgeCapture()
  const mock = vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/knowledge/captures/observation')) {
      return jsonResponse({ error: { code: 'not_found', message: 'no route' } }, 404)
    }
    throw new Error(`Unexpected request: ${String(input)}`)
  })
  vi.stubGlobal('fetch', mock)

  render(
    <CaptureDrawer
      capture={listed}
      onClose={vi.fn()}
      onCreateTask={vi.fn()}
      onReviewKnowledge={vi.fn()}
      workspace={workspace}
    />,
  )
  await screen.findByText('Saved checks are unavailable on this server.')

  // R27's own history read is the only request an open drawer makes. The new entry adds
  // none of its own — it waits for a click.
  expect(screen.getByRole('button', { name: 'Search for updated context' })).toBeInTheDocument()
  for (const [input] of mock.mock.calls) {
    expect(String(input)).toMatch(/^\/api\/v1\/knowledge\/captures\/observation/)
  }
})
