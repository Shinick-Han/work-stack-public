import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { CaptureImportDialog } from './CaptureImportDialog'
import { knowledgeImportEnvelope } from './knowledgeCaptureFixture'

const manualPacket = {
  schema_version: '1.0',
  source_key: `sha256:${'a'.repeat(64)}`,
  source: {
    provider: 'manual',
    resource_type: 'manual.note',
    connection_ref: 'local-user',
    container_ref: 'manual-inbox',
    object_ref: 'manual:test',
    version_ref: 'manual:v1',
    display_title: 'Sanitized note',
    web_url: null,
    retrieved_at: '2026-08-29T08:30:00Z',
    fingerprint: `sha256:${'b'.repeat(64)}`,
  },
  normalized: { summary: 'Safe summary.', context: 'Safe context.', action_items: [], tags: ['manual'] },
  task_hints: [],
  provenance: {
    capture_mode: 'manual',
    adapter: 'manual-import',
    adapter_version: '1.0.0',
    redaction_policy_version: 'workstack-redaction-v1',
    raw_retained: false,
    created_at: '2026-08-29T08:30:03Z',
  },
}

test('manual packet submit stays 1.0 and does not open a nested dialog', async () => {
  const onSubmit = vi.fn()
  render(
    <CaptureImportDialog
      onClose={vi.fn()}
      onSubmit={onSubmit}
      open
      pending={false}
      serverError={null}
    />,
  )
  expect(screen.getByRole('dialog', { name: 'Import context' })).toBeInTheDocument()
  expect(screen.queryByRole('dialog', { name: /knowledge/i })).not.toBeInTheDocument()
  expect(screen.getByText(/sanitized context only/i)).toBeInTheDocument()
  expect(screen.queryByText(/Microsoft 365 remains the source of truth/i)).not.toBeInTheDocument()

  fireEvent.change(screen.getByLabelText('Capture Packet v1 JSON'), {
    target: { value: JSON.stringify(manualPacket) },
  })
  await userEvent.click(screen.getByRole('button', { name: 'Import packet' }))
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ schema_version: '1.0', provenance: expect.objectContaining({ capture_mode: 'manual' }) }))
})

test('knowledge mode previews then requires an explicit Import into Inbox', async () => {
  const onSubmit = vi.fn()
  const onKnowledgeImported = vi.fn()
  render(
    <CaptureImportDialog
      onClose={vi.fn()}
      onKnowledgeImported={onKnowledgeImported}
      onSubmit={onSubmit}
      open
      pending={false}
      serverError={null}
    />,
  )
  await userEvent.click(screen.getByRole('tab', { name: 'Knowledge result' }))
  fireEvent.change(
    screen.getByLabelText('Knowledge result envelope'),
    { target: { value: JSON.stringify(knowledgeImportEnvelope()) } },
  )
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  expect(screen.getByText(/not a correctness probability/i)).toBeInTheDocument()
  expect(onSubmit).not.toHaveBeenCalled()
  expect(onKnowledgeImported).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Import into Inbox' })).toBeEnabled()
})

test('prefill opens knowledge preview without posting until Import is pressed', async () => {
  const onSubmit = vi.fn()
  const onKnowledgeImported = vi.fn()
  const envelope = knowledgeImportEnvelope()
  render(
    <CaptureImportDialog
      onClose={vi.fn()}
      onKnowledgeImported={onKnowledgeImported}
      onSubmit={onSubmit}
      open
      pending={false}
      prefill={envelope}
      serverError={null}
    />,
  )
  expect(screen.getByRole('tab', { name: 'Knowledge result' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  expect(onSubmit).not.toHaveBeenCalled()
  expect(onKnowledgeImported).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Import into Inbox' })).toBeEnabled()
})

test('clearing a non-busy execute prefill closes the import so the stale envelope cannot submit', () => {
  const onClose = vi.fn()
  const envelope = knowledgeImportEnvelope()
  const { rerender } = render(
    <CaptureImportDialog
      onClose={onClose}
      onSubmit={vi.fn()}
      open
      pending={false}
      prefill={envelope}
      serverError={null}
    />,
  )
  expect(screen.getByText('Rollback verification owner')).toBeInTheDocument()
  rerender(
    <CaptureImportDialog
      onClose={onClose}
      onSubmit={vi.fn()}
      open
      pending={false}
      prefill={null}
      serverError={null}
    />,
  )
  expect(onClose).toHaveBeenCalledTimes(1)
})
