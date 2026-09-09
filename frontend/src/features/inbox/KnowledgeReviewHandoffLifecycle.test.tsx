import { useState } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'
import { UnreadableSuccessError } from '../../api/transport'
import { CaptureImportDialog } from './CaptureImportDialog'
import { REQUEST_ID, knowledgeImportEnvelope, retrievalWire } from './knowledgeCaptureFixture'
import { useKnowledgeReviewHandoff } from './useKnowledgeReviewHandoff'

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

const WORKSPACE_A = '22222222-2222-2222-2222-222222222222'
const WORKSPACE_B = '33333333-3333-4333-8333-333333333333'
const OTHER_REQUEST_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'

/** A later request's envelope, self-consistent so review can admit it. */
function laterEnvelope() {
  const [item] = knowledgeImportEnvelope().items
  return knowledgeImportEnvelope({
    request_id: OTHER_REQUEST_ID,
    items: [{ ...item, retrieval: retrievalWire({ request_id: OTHER_REQUEST_ID }) }],
  })
}

beforeEach(() => {
  importKnowledgeCaptures.mockReset()
})

function ComposedHandoff({
  onImported = vi.fn(),
  workspaceUid,
}: {
  onImported?: (outcome: unknown) => void
  workspaceUid: string
}) {
  const [importOpen, setImportOpen] = useState(false)
  const review = useKnowledgeReviewHandoff({
    importOpen,
    importPending: false,
    setImportOpen,
    workspaceUid,
  })
  return (
    <>
      <button type="button" onClick={() => setImportOpen(true)}>Open import</button>
      <button type="button" onClick={() => review.onReviewKnowledge(knowledgeImportEnvelope())}>
        Hand off original
      </button>
      <button
        type="button"
        onClick={() => review.onReviewKnowledge(knowledgeImportEnvelope({ request_id: OTHER_REQUEST_ID }))}
      >
        Hand off other
      </button>
      <button type="button" onClick={() => review.onReviewKnowledge(laterEnvelope())}>
        Hand off later request
      </button>
      <CaptureImportDialog
        onClose={() => setImportOpen(false)}
        onKnowledgeImported={onImported}
        onKnowledgeStatusChange={review.onKnowledgeStatusChange}
        onSubmit={vi.fn()}
        open={importOpen}
        pending={false}
        prefill={review.prefill}
        serverError={null}
      />
    </>
  )
}

test('workspace change closes a non-busy execute-prefilled import so the stale envelope cannot submit', async () => {
  const { rerender } = render(<ComposedHandoff workspaceUid={WORKSPACE_A} />)
  await userEvent.click(screen.getByRole('button', { name: 'Hand off original' }))
  expect(screen.getByRole('dialog', { name: 'Import context' })).toBeInTheDocument()
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()

  rerender(<ComposedHandoff workspaceUid={WORKSPACE_B} />)
  expect(screen.queryByRole('dialog', { name: 'Import context' })).not.toBeInTheDocument()
  expect(screen.queryByText('Rollback verification owner')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Import into Inbox' })).not.toBeInTheDocument()
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
})

test('workspace change keeps a frozen identical retry and refuses a replacement handoff', async () => {
  const onImported = vi.fn()
  importKnowledgeCaptures.mockRejectedValueOnce(new UnreadableSuccessError(200))
  const { rerender } = render(<ComposedHandoff onImported={onImported} workspaceUid={WORKSPACE_A} />)
  await userEvent.click(screen.getByRole('button', { name: 'Hand off original' }))
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Retry the same envelope/i))
  const firstBody = importKnowledgeCaptures.mock.calls[0][0]

  rerender(<ComposedHandoff onImported={onImported} workspaceUid={WORKSPACE_B} />)
  expect(screen.getByRole('dialog', { name: 'Import context' })).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent(/Retry the same envelope/i)
  fireEvent.click(screen.getByRole('button', { name: 'Hand off other' }))
  expect(screen.getByLabelText('Knowledge result envelope')).toBeDisabled()

  importKnowledgeCaptures.mockResolvedValueOnce({
    data: {
      request_id: firstBody.request_id,
      capture_ids: ['C-0008'],
      completion_digest: `sha256:${'d'.repeat(64)}`,
      completed_at: '2026-09-08T11:00:00Z',
    },
    meta: { replayed: true, imported_count: 1 },
  })
  await userEvent.click(screen.getByRole('button', { name: 'Retry same import' }))
  await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1))
  expect(importKnowledgeCaptures).toHaveBeenCalledTimes(2)
  expect(importKnowledgeCaptures.mock.calls[1][0]).toBe(firstBody)
  expect(firstBody.request_id).not.toBe(OTHER_REQUEST_ID)
})

test('workspace change leaves a normal manual knowledge paste open and submittable', async () => {
  const { rerender } = render(<ComposedHandoff workspaceUid={WORKSPACE_A} />)
  await userEvent.click(screen.getByRole('button', { name: 'Open import' }))
  await userEvent.click(screen.getByRole('tab', { name: 'Knowledge result' }))
  fireEvent.change(screen.getByLabelText('Knowledge result envelope'), {
    target: { value: JSON.stringify(knowledgeImportEnvelope()) },
  })
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()

  rerender(<ComposedHandoff workspaceUid={WORKSPACE_B} />)
  expect(screen.getByRole('dialog', { name: 'Import context' })).toBeInTheDocument()
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Import into Inbox' })).toBeEnabled()
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
})

test('an explicitly cancelled unknown import releases the guard so a later handoff still opens review', async () => {
  const onImported = vi.fn()
  importKnowledgeCaptures.mockRejectedValueOnce(new UnreadableSuccessError(200))
  render(<ComposedHandoff onImported={onImported} workspaceUid={WORKSPACE_A} />)
  await userEvent.click(screen.getByRole('button', { name: 'Hand off original' }))
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Retry the same envelope/i))
  expect(importKnowledgeCaptures).toHaveBeenCalledTimes(1)

  // An unknown outcome is frozen, not busy, so Cancel is a real user choice here.
  const cancel = screen.getByRole('button', { name: 'Cancel' })
  expect(cancel).toBeEnabled()
  await userEvent.click(cancel)
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Import context' })).not.toBeInTheDocument())

  // A later executed request must still reach review; the abandoned import may
  // not keep the handoff guard held.
  await userEvent.click(screen.getByRole('button', { name: 'Hand off later request' }))
  expect(screen.getByRole('dialog', { name: 'Import context' })).toBeInTheDocument()
  const envelopeField = screen.getByLabelText('Knowledge result envelope') as HTMLTextAreaElement
  await waitFor(() => expect(envelopeField.value).toContain(OTHER_REQUEST_ID))
  expect(envelopeField.value).not.toContain(REQUEST_ID)
  expect(envelopeField).toBeEnabled()

  // Review only opened: nothing was imported or executed on the user's behalf.
  expect(importKnowledgeCaptures).toHaveBeenCalledTimes(1)
  expect(onImported).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Import into Inbox' })).toBeEnabled()
})
