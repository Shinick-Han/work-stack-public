import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'
import { UnreadableSuccessError } from '../../api/transport'
import { KnowledgeCaptureImportForm } from './KnowledgeCaptureImportForm'
import type { KnowledgeImportEnvelope } from './knowledgeCaptureImport'
import { knowledgeImportEnvelope } from './knowledgeCaptureFixture'

const importKnowledgeCaptures = vi.fn()

vi.mock('../../api/knowledgeCapture', async () => {
  const actual = await vi.importActual<typeof import('../../api/knowledgeCapture')>('../../api/knowledgeCapture')
  return {
    ...actual,
    importKnowledgeCaptures: (...args: unknown[]) => importKnowledgeCaptures(...args),
  }
})

beforeEach(() => {
  importKnowledgeCaptures.mockReset()
})

test('explicit import posts once and notifies only on success', async () => {
  importKnowledgeCaptures.mockResolvedValue({
    data: {
      request_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      capture_ids: ['C-0008'],
      completion_digest: `sha256:${'d'.repeat(64)}`,
      completed_at: '2026-09-08T11:00:00Z',
    },
    meta: { replayed: false, imported_count: 1 },
  })
  const onImported = vi.fn()
  render(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={onImported}
        onStatusChange={vi.fn()}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  fireEvent.change(screen.getByLabelText('Knowledge result envelope'), {
    target: { value: JSON.stringify(knowledgeImportEnvelope()) },
  })
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1))
  expect(importKnowledgeCaptures).toHaveBeenCalledTimes(1)
})

test('transport loss retries the same admitted envelope and does not mint a new request', async () => {
  const envelope = knowledgeImportEnvelope()
  importKnowledgeCaptures
    .mockRejectedValueOnce(new UnreadableSuccessError(200))
    .mockResolvedValueOnce({
      data: {
        request_id: envelope.request_id,
        capture_ids: ['C-0008'],
        completion_digest: `sha256:${'d'.repeat(64)}`,
        completed_at: '2026-09-08T11:00:00Z',
      },
      meta: { replayed: true, imported_count: 1 },
    })
  const onImported = vi.fn()
  render(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={onImported}
        onStatusChange={vi.fn()}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  fireEvent.change(screen.getByLabelText('Knowledge result envelope'), {
    target: { value: JSON.stringify(envelope) },
  })
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Retry the same envelope/i))
  expect(onImported).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Knowledge result envelope')).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1))
  expect(importKnowledgeCaptures).toHaveBeenCalledTimes(2)
  expect(importKnowledgeCaptures.mock.calls[1][0]).toBe(importKnowledgeCaptures.mock.calls[0][0])
})

test('does not create a Task on import or retry', async () => {
  importKnowledgeCaptures.mockRejectedValueOnce(new UnreadableSuccessError(200))
  render(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={vi.fn()}
        onStatusChange={vi.fn()}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  fireEvent.change(screen.getByLabelText('Knowledge result envelope'), {
    target: { value: JSON.stringify(knowledgeImportEnvelope()) },
  })
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  expect(screen.queryByRole('button', { name: /create/i })).not.toBeInTheDocument()
})

test('prefill is previewed without posting, and a later handoff does not reset a frozen retry', async () => {
  const envelope = knowledgeImportEnvelope()
  importKnowledgeCaptures.mockRejectedValueOnce(new UnreadableSuccessError(200))
  const onImported = vi.fn()
  const onStatusChange = vi.fn()
  const { rerender } = render(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={onImported}
        onStatusChange={onStatusChange}
        prefill={envelope}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Retry the same envelope/i))
  const firstBody = importKnowledgeCaptures.mock.calls[0][0]
  const other = knowledgeImportEnvelope({
    request_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
  })
  rerender(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={onImported}
        onStatusChange={onStatusChange}
        prefill={other}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  importKnowledgeCaptures.mockResolvedValueOnce({
    data: {
      request_id: envelope.request_id,
      capture_ids: ['C-0008'],
      completion_digest: `sha256:${'d'.repeat(64)}`,
      completed_at: '2026-09-08T11:00:00Z',
    },
    meta: { replayed: true, imported_count: 1 },
  })
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1))
  expect(importKnowledgeCaptures).toHaveBeenCalledTimes(2)
  expect(importKnowledgeCaptures.mock.calls[1][0]).toBe(firstBody)
  expect(importKnowledgeCaptures.mock.calls[1][0].request_id).toBe(envelope.request_id)
})

test('dropping a non-busy execute prefill clears copied preview so it cannot submit', async () => {
  const envelope = knowledgeImportEnvelope()
  const { rerender } = render(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={vi.fn()}
        onStatusChange={vi.fn()}
        prefill={envelope}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  rerender(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={vi.fn()}
        onStatusChange={vi.fn()}
        prefill={null}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  expect(screen.queryByText('Rollback verification owner')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Knowledge result envelope')).toHaveValue('')
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
})

test('dropping execute prefill during frozen retry keeps identical retry semantics', async () => {
  const envelope = knowledgeImportEnvelope()
  importKnowledgeCaptures.mockRejectedValueOnce(new UnreadableSuccessError(200))
  const onImported = vi.fn()
  const { rerender } = render(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={onImported}
        onStatusChange={vi.fn()}
        prefill={envelope}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Retry the same envelope/i))
  const firstBody = importKnowledgeCaptures.mock.calls[0][0]
  rerender(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={onImported}
        onStatusChange={vi.fn()}
        prefill={null}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  expect(screen.getByRole('status')).toHaveTextContent(/Retry the same envelope/i)
  expect(screen.getByLabelText('Knowledge result envelope')).toBeDisabled()
  importKnowledgeCaptures.mockResolvedValueOnce({
    data: {
      request_id: envelope.request_id,
      capture_ids: ['C-0008'],
      completion_digest: `sha256:${'d'.repeat(64)}`,
      completed_at: '2026-09-08T11:00:00Z',
    },
    meta: { replayed: true, imported_count: 1 },
  })
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1))
  expect(importKnowledgeCaptures).toHaveBeenCalledTimes(2)
  expect(importKnowledgeCaptures.mock.calls[1][0]).toBe(firstBody)
})

test('manual knowledge entry is not cleared when execute prefill stays absent', () => {
  const envelope = knowledgeImportEnvelope()
  const { rerender } = render(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={vi.fn()}
        onStatusChange={vi.fn()}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  fireEvent.change(screen.getByLabelText('Knowledge result envelope'), {
    target: { value: JSON.stringify(envelope) },
  })
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  rerender(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={vi.fn()}
        onStatusChange={vi.fn()}
        prefill={null}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  expect(screen.getByLabelText('Knowledge result envelope')).toHaveValue(JSON.stringify(envelope))
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
})


test('hostile summary is not shown in the preview', () => {
  render(
    <KnowledgeCaptureImportForm
      formId="knowledge-import-form"
      locked={false}
      open
      onImported={vi.fn()}
      onStatusChange={vi.fn()}
    />,
  )
  const canary = 'RAW_CANARY_DO_NOT_STORE in the answer'
  const item = knowledgeImportEnvelope().items[0]
  fireEvent.change(screen.getByLabelText('Knowledge result envelope'), {
    target: {
      value: JSON.stringify(knowledgeImportEnvelope({
        items: [{ ...item, normalized: { ...item.normalized, summary: canary } }],
      })),
    },
  })
  expect(document.querySelector('.knowledge-import-preview')).toBeNull()
  expect(screen.queryByText(canary, { selector: 'p' })).not.toBeInTheDocument()
})

const importForm = () => document.querySelector('.knowledge-import') as HTMLElement
const rawDisclosure = () => document.querySelector('details.knowledge-import__raw')
const previews = () => document.querySelectorAll('.knowledge-import-preview')

function renderForm(prefill: KnowledgeImportEnvelope | null) {
  return render(
    <>
      <KnowledgeCaptureImportForm
        formId="knowledge-import-form"
        locked={false}
        open
        onImported={vi.fn()}
        onStatusChange={vi.fn()}
        prefill={prefill}
      />
      <button form="knowledge-import-form" type="submit">Import into Inbox</button>
    </>,
  )
}

test('an execution result is readable first and keeps its envelope behind one collapsed disclosure', () => {
  renderForm(knowledgeImportEnvelope())
  const raw = rawDisclosure()
  expect(raw).not.toBeNull()
  expect(raw).not.toHaveAttribute('open')
  expect(screen.getByText('Show the raw envelope').tagName).toBe('SUMMARY')
  expect(screen.getByText(/These are the results your search returned/)).toBeInTheDocument()
  expect(screen.queryByText(/Paste the frozen knowledge result envelope/)).not.toBeInTheDocument()
  // The result the user reads leads; the JSON they no longer have to read follows it.
  expect(previews()).toHaveLength(1)
  const preview = previews()[0]
  expect(preview.compareDocumentPosition(raw as Node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(importForm().firstElementChild?.textContent).toMatch(/These are the results your search returned/)
  // One textarea, and it is the one inside the disclosure.
  const envelopeField = screen.getByLabelText('Knowledge result envelope')
  expect(document.querySelectorAll('textarea')).toHaveLength(1)
  expect(raw?.contains(envelopeField)).toBe(true)
  expect(raw?.contains(document.querySelector('input[type="file"]') as Node)).toBe(true)
  expect(envelopeField).not.toBeDisabled()
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
})

test('opening the raw envelope neither imports nor duplicates the readable result', async () => {
  renderForm(knowledgeImportEnvelope())
  await userEvent.click(screen.getByText('Show the raw envelope'))
  expect(rawDisclosure()).toHaveAttribute('open')
  expect(screen.getByLabelText('Knowledge result envelope')).toHaveValue(
    JSON.stringify(knowledgeImportEnvelope(), null, 2),
  )
  expect(previews()).toHaveLength(1)
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
})

test('a hand-pasted import keeps the textarea first-class and collapses nothing', () => {
  renderForm(null)
  expect(rawDisclosure()).toBeNull()
  expect(document.querySelector('details')).toBeNull()
  expect(screen.getByText(/Paste the frozen knowledge result envelope/)).toBeInTheDocument()
  expect(screen.queryByText(/These are the results your search returned/)).not.toBeInTheDocument()
  const envelopeField = screen.getByLabelText('Knowledge result envelope')
  expect(envelopeField).not.toBeDisabled()
  expect(document.querySelector('input[type="file"]')).not.toBeNull()
  fireEvent.change(envelopeField, { target: { value: JSON.stringify(knowledgeImportEnvelope()) } })
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  expect(rawDisclosure()).toBeNull()
})

test('a refused execution result shows its refusal outside the disclosure and cannot be imported', async () => {
  renderForm(knowledgeImportEnvelope({ request_id: 'not-a-canonical-uuid' }))
  const raw = rawDisclosure()
  const refusal = screen.getByRole('alert')
  expect(refusal).toBeInTheDocument()
  expect(raw?.contains(refusal)).toBe(false)
  // Nothing readable stands in for the envelope, so the correction surface is already open.
  expect(previews()).toHaveLength(0)
  expect(raw).toHaveAttribute('open')
  expect(screen.getByLabelText('Knowledge result envelope')).toHaveValue('')
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  expect(importKnowledgeCaptures).not.toHaveBeenCalled()
})

test('a refused result never renders its own hostile text', () => {
  const canary = 'RAW_CANARY_DO_NOT_STORE in the answer'
  const item = knowledgeImportEnvelope().items[0]
  renderForm(knowledgeImportEnvelope({
    items: [{ ...item, normalized: { ...item.normalized, summary: canary } }],
  }))
  expect(screen.getByRole('alert')).toBeInTheDocument()
  expect(previews()).toHaveLength(0)
  expect(screen.queryByText(canary, { selector: 'p' })).not.toBeInTheDocument()
  expect(document.querySelector('.knowledge-import')?.innerHTML).not.toContain('<script')
})

test('an unknown outcome reports itself above the disclosure and freezes the raw controls', async () => {
  importKnowledgeCaptures.mockRejectedValueOnce(new UnreadableSuccessError(200))
  renderForm(knowledgeImportEnvelope())
  await userEvent.click(screen.getByRole('button', { name: 'Import into Inbox' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/Retry the same envelope/i))
  const raw = rawDisclosure()
  expect(raw?.contains(screen.getByRole('status'))).toBe(false)
  expect(screen.queryByText(/imported|saved to your Inbox/i)).not.toBeInTheDocument()
  expect(screen.getByLabelText('Knowledge result envelope')).toBeDisabled()
  expect(document.querySelector('input[type="file"]')).toBeDisabled()
  expect(previews()).toHaveLength(1)
})

test('closing the window drops the execution layout for the next manual import', () => {
  const { rerender } = renderForm(knowledgeImportEnvelope())
  expect(rawDisclosure()).not.toBeNull()
  rerender(
    <KnowledgeCaptureImportForm
      formId="knowledge-import-form"
      locked={false}
      open={false}
      onImported={vi.fn()}
      onStatusChange={vi.fn()}
      prefill={null}
    />,
  )
  rerender(
    <KnowledgeCaptureImportForm
      formId="knowledge-import-form"
      locked={false}
      open
      onImported={vi.fn()}
      onStatusChange={vi.fn()}
      prefill={null}
    />,
  )
  expect(rawDisclosure()).toBeNull()
  expect(screen.getByLabelText('Knowledge result envelope')).toHaveValue('')
  expect(screen.getByText(/Paste the frozen knowledge result envelope/)).toBeInTheDocument()
})
