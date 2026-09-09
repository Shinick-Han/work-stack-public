import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { capture, jsonResponse, workspace } from '../../test/fixtures'
import { CaptureDrawer } from './CaptureDrawer'
import { CaptureImportDialog } from './CaptureImportDialog'
import { KnowledgeRequestLauncher } from './KnowledgeRequestLauncher'
import { knowledgeImportEnvelope, retrievalWire } from './knowledgeCaptureFixture'
import type { KnowledgeImportEnvelope } from './knowledgeCaptureImport'

const importKnowledgeCaptures = vi.fn()

vi.mock('../../api/knowledgeCapture', async () => {
  const actual = await vi.importActual<typeof import('../../api/knowledgeCapture')>(
    '../../api/knowledgeCapture',
  )
  return {
    ...actual,
    importKnowledgeCaptures: (...args: unknown[]) => importKnowledgeCaptures(...args),
  }
})

const WORKSPACE_UID = '22222222-2222-2222-2222-222222222222'
const UPSTREAM = '66666666-6666-4666-8666-666666666666'
const ISSUED_REQUEST_ID = '2cce8f7c-7961-5614-bc6f-ca4a093661e2'
const EXECUTE_PATH = '/api/v1/knowledge/requests/execute'

const teamNas = {
  alias: 'team-nas',
  corpus_refs: ['nas-team-share', 'product-notes'],
  scope: 'workspace' as const,
  upstream_workspace_uid: UPSTREAM,
}

function proposalFor(requestId: string): KnowledgeImportEnvelope {
  const item = knowledgeImportEnvelope().items[0]
  return knowledgeImportEnvelope({
    request_id: requestId,
    items: [{ ...item, retrieval: retrievalWire({ request_id: requestId }) }],
  })
}

function stubKnowledgeServer() {
  const executeBodies: unknown[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const raw = init?.body
      const body = typeof raw === 'string' && raw ? JSON.parse(raw) as Record<string, unknown> : null
      if (url.includes('/api/v1/session')) {
        return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
      }
      if (url === '/api/v1/knowledge/connections' && method === 'GET') {
        return jsonResponse({ data: { connections: [teamNas], policy_revision: 1 } })
      }
      if (url === '/api/v1/knowledge/requests') {
        return jsonResponse({
          data: {
            binding: body?.binding,
            corpus_refs: body?.corpus_refs,
            expires_at: '2099-09-08T09:37:07Z',
            purpose: body?.purpose,
            query: body?.query,
            request_id: ISSUED_REQUEST_ID,
            requested_at: '2099-09-08T09:32:07Z',
            result_limit: body?.result_limit,
            schema: 'workstack.knowledge-request.v1',
          },
          meta: {
            connection_alias: 'team-nas',
            policy_revision: 1,
            replayed: false,
            state: 'pending',
          },
        })
      }
      if (url === EXECUTE_PATH) {
        executeBodies.push(body)
        return jsonResponse({
          data: proposalFor(ISSUED_REQUEST_ID),
          meta: { outcome: 'proposal_ready' },
        })
      }
      throw new Error(`Unexpected request: ${url}`)
    }),
  )
  return executeBodies
}

function ReviewHandoff({ onImported }: { onImported: (outcome: unknown) => void }) {
  const [prefill, setPrefill] = useState<KnowledgeImportEnvelope | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  return (
    <>
      <KnowledgeRequestLauncher
        newIntentId={() => 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}
        onReviewKnowledge={(envelope) => {
          setPrefill(envelope)
          setImportOpen(true)
        }}
        workspaceUid={WORKSPACE_UID}
      />
      <CaptureImportDialog
        onClose={() => setImportOpen(false)}
        onKnowledgeImported={onImported}
        onSubmit={vi.fn()}
        open={importOpen}
        pending={false}
        prefill={prefill}
        serverError={null}
      />
    </>
  )
}

test('a valid execute result opens prefilled review and does not import until confirmation', async () => {
  const user = userEvent.setup()
  const executeBodies = stubKnowledgeServer()
  const onImported = vi.fn()
  importKnowledgeCaptures.mockResolvedValue({
    data: {
      request_id: ISSUED_REQUEST_ID,
      capture_ids: ['C-0008'],
      completion_digest: `sha256:${'d'.repeat(64)}`,
      completed_at: '2026-09-08T11:00:00Z',
    },
    meta: { replayed: false, imported_count: 1 },
  })
  render(<ReviewHandoff onImported={onImported} />)

  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await user.type(await screen.findByLabelText('Question'), 'rollback verification owner')
  await user.click(screen.getByRole('checkbox', { name: /nas-team-share/ }))
  await user.click(screen.getByRole('button', { name: 'Generate request' }))
  expect(await screen.findByText('Issued request')).toBeInTheDocument()
  expect(executeBodies).toHaveLength(0)

  await user.click(screen.getByRole('button', { name: 'Run connected search' }))
  await waitFor(() => expect(executeBodies).toHaveLength(1))
  expect(executeBodies[0]).toEqual(expect.objectContaining({
    request_id: ISSUED_REQUEST_ID,
    query: 'rollback verification owner',
  }))
  await user.click(await screen.findByRole('button', { name: 'Review results' }))

  expect(screen.queryByRole('dialog', { name: 'Ask a scoped knowledge request' })).not.toBeInTheDocument()
  expect(await screen.findByRole('dialog', { name: 'Import context' })).toBeInTheDocument()
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
  expect(onImported).not.toHaveBeenCalled()

  await user.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1))
  expect(importKnowledgeCaptures).toHaveBeenCalledTimes(1)
  expect(importKnowledgeCaptures.mock.calls[0][0].request_id).toBe(ISSUED_REQUEST_ID)
})

test('rapid execute clicks still post once on the issued receipt', async () => {
  stubKnowledgeServer()
  render(
    <KnowledgeRequestLauncher
      newIntentId={() => 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}
      workspaceUid={WORKSPACE_UID}
    />,
  )
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await user.type(await screen.findByLabelText('Question'), 'rollback verification owner')
  await user.click(screen.getByRole('checkbox', { name: /nas-team-share/ }))
  await user.click(screen.getByRole('button', { name: 'Generate request' }))
  await screen.findByText('Issued request')
  const run = screen.getByRole('button', { name: 'Run connected search' })
  fireEvent.click(run)
  fireEvent.click(run)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Review results' })).toBeInTheDocument())
  const fetchMock = vi.mocked(fetch)
  const executePosts = fetchMock.mock.calls.filter(([input, init]) => (
    String(input) === EXECUTE_PATH && (init as RequestInit | undefined)?.method === 'POST'
  ))
  expect(executePosts).toHaveLength(1)
})

/**
 * R31-A: the same execute→review→import path, entered from a saved Capture.
 *
 * The point of this test is that the drawer entry reaches the *existing* review preview
 * with the actual proposal, and that getting there changes nothing about the Capture it
 * started from: no capture route is written, and the import still waits for the user.
 */
function CaptureDrawerHandoff({ onImported }: { onImported: (outcome: unknown) => void }) {
  const [prefill, setPrefill] = useState<KnowledgeImportEnvelope | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  return (
    <>
      <CaptureDrawer
        capture={capture}
        onClose={vi.fn()}
        onCreateTask={vi.fn()}
        onReviewKnowledge={(envelope) => {
          setPrefill(envelope)
          setImportOpen(true)
        }}
        workspace={workspace}
      />
      <CaptureImportDialog
        onClose={() => setImportOpen(false)}
        onKnowledgeImported={onImported}
        onSubmit={vi.fn()}
        open={importOpen}
        pending={false}
        prefill={prefill}
        serverError={null}
      />
    </>
  )
}

test('a search started from a Capture reaches the existing review preview, unchanged Capture', async () => {
  const user = userEvent.setup()
  const executeBodies = stubKnowledgeServer()
  const onImported = vi.fn()
  importKnowledgeCaptures.mockResolvedValue({
    data: {
      request_id: ISSUED_REQUEST_ID,
      capture_ids: ['C-0008'],
      completion_digest: `sha256:${'d'.repeat(64)}`,
      completed_at: '2026-09-08T11:00:00Z',
    },
    meta: { replayed: false, imported_count: 1 },
  })
  const before = JSON.stringify(capture)
  render(<CaptureDrawerHandoff onImported={onImported} />)

  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  // This Capture's own alias is not one the owner grants, so the roster decides.
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  const question = await screen.findByLabelText('Question')
  expect(question).toHaveValue('Release review feedback')
  expect(screen.getByLabelText('Purpose')).toHaveValue('find_context')

  await user.click(screen.getByRole('checkbox', { name: /nas-team-share/ }))
  await user.click(screen.getByRole('button', { name: 'Generate request' }))
  expect(await screen.findByText('Issued request')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Run connected search' }))
  await waitFor(() => expect(executeBodies).toHaveLength(1))
  expect(executeBodies[0]).toEqual(expect.objectContaining({
    request_id: ISSUED_REQUEST_ID,
    query: 'Release review feedback',
  }))
  await user.click(await screen.findByRole('button', { name: 'Review results' }))

  // The existing import preview, holding the actual proposal.
  expect(await screen.findByRole('dialog', { name: 'Import context' })).toBeInTheDocument()
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  // Nothing has been imported, and the Capture this started from is untouched: no capture
  // route was written, and the object on screen is the one the caller handed in.
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
  expect(JSON.stringify(capture)).toBe(before)
  const writes = vi.mocked(fetch).mock.calls.filter(([input, init]) => (
    String(input).startsWith('/api/v1/captures') && (init as RequestInit | undefined)?.method === 'POST'
  ))
  expect(writes).toHaveLength(0)

  // Import stays the user's explicit step, exactly as it was from the Inbox entry.
  await user.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1))
  expect(importKnowledgeCaptures).toHaveBeenCalledTimes(1)
  expect(importKnowledgeCaptures.mock.calls[0][0].request_id).toBe(ISSUED_REQUEST_ID)
})
