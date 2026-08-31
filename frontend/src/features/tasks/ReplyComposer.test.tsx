import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import type { ReplyCommand, ReplyReceipt } from '../../domain/types'
import { ReplyComposer, parseReplyReceiptText, type ReplySource } from './ReplyComposer'

const bodyDigest = `sha256:${'a'.repeat(64)}`
const targetDigest = `sha256:${'b'.repeat(64)}`
const source: ReplySource = {
  capture_id: 'C-0001',
  provider: 'microsoft-outlook',
  resource_type: 'mail.message',
  connection_ref: 'connection:opaque',
  container_ref: 'mailbox:opaque',
  display_title: 'Release review feedback',
  object_ref: 'message:opaque',
  version_ref: 'change-key:v1',
}
const command: ReplyCommand = {
  id: 'R-0001',
  task_id: 'T-0001',
  capture_id: source.capture_id,
  capture_revision: 1,
  provider: source.provider,
  capability: 'outlook.reply',
  target: {
    resource_type: source.resource_type,
    connection_ref: source.connection_ref,
    container_ref: source.container_ref,
    object_ref: source.object_ref,
    version_ref: source.version_ref,
  },
  body: 'Thanks. I will add the rollback check.',
  body_digest: bodyDigest,
  target_digest: targetDigest,
  state: 'approved',
  approved_at: '2026-08-29T09:00:00Z',
  receipt: null,
  created_at: '2026-08-29T09:00:00Z',
  updated_at: '2026-08-29T09:00:00Z',
}
const receipt: ReplyReceipt = {
  schema_version: '1.0',
  reply_id: command.id,
  provider: command.provider,
  outcome: 'sent',
  occurred_at: '2026-08-29T09:05:00Z',
  body_digest: bodyDigest,
  target_digest: targetDigest,
}

test('creates no command before approval, then copies it and imports a matching receipt', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  const onCreate = vi.fn().mockResolvedValue(command)
  const onImportReceipt = vi.fn().mockResolvedValue({
    ...command,
    state: 'sent',
    receipt,
    updated_at: receipt.occurred_at,
  })
  render(
    <ReplyComposer
      onCreate={onCreate}
      onImportReceipt={onImportReceipt}
      replies={[]}
      sources={[source]}
      taskId="T-0001"
    />,
  )

  const approveButton = screen.getByRole('button', { name: 'Approve reply command' })
  expect(approveButton).toBeDisabled()
  expect(onCreate).not.toHaveBeenCalled()

  await userEvent.type(screen.getByLabelText('Plain-text reply'), command.body)
  expect(screen.getAllByText(command.body)).toHaveLength(2)
  const approvalTarget = screen.getByLabelText('Exact reply target')
  for (const value of Object.values(command.target)) expect(within(approvalTarget).getByText(value)).toBeInTheDocument()
  await userEvent.click(screen.getByLabelText(/i approve this exact target/i))
  await userEvent.click(approveButton)
  expect(onCreate).toHaveBeenCalledWith({
    task_id: 'T-0001',
    capture_id: 'C-0001',
    body: command.body,
    approved: true,
  })

  const frozenTarget = await screen.findByLabelText('Exact reply target')
  for (const value of Object.values(command.target)) expect(within(frozenTarget).getByText(value)).toBeInTheDocument()

  await userEvent.click(await screen.findByRole('button', { name: 'Copy approved command' }))
  expect(writeText.mock.calls[0][0]).toContain('Do not change the target or body')
  expect(writeText.mock.calls[0][0]).toContain('"id": "R-0001"')

  fireEvent.change(screen.getByLabelText('Agent receipt'), { target: { value: JSON.stringify(receipt) } })
  await userEvent.click(screen.getByRole('button', { name: 'Import matching receipt' }))
  expect(onImportReceipt).toHaveBeenCalledWith('R-0001', receipt)
  expect(await screen.findByText(/records this reply as sent/i)).toBeInTheDocument()
})

test('strict receipt parsing rejects arbitrary connector output', () => {
  expect(() => parseReplyReceiptText(JSON.stringify({ ...receipt, connector_output: { debug: true } }))).toThrow()
})

test('resumes a stored approved command instead of creating a duplicate draft', async () => {
  render(
    <ReplyComposer
      onCreate={vi.fn()}
      onImportReceipt={vi.fn()}
      replies={[command]}
      sources={[source]}
      taskId="T-0001"
    />,
  )

  expect(await screen.findByRole('button', { name: 'Copy approved command' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Approve reply command' })).not.toBeInTheDocument()
  expect(screen.getByText(/not yet recorded as sent/i)).toBeInTheDocument()
})
