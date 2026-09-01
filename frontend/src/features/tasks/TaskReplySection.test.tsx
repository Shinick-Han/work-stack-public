import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import type { ReplySource } from './ReplyComposer'
import { TaskReplySection } from './TaskReplySection'

const source: ReplySource = {
  capture_id: 'C-0001',
  provider: 'microsoft-outlook',
  resource_type: 'mail.message',
  connection_ref: 'outlook',
  container_ref: 'inbox',
  display_title: 'Release review',
  object_ref: 'message:1',
  version_ref: 'v1',
}

test('delegates composer visibility and explains unavailable sources', async () => {
  const user = userEvent.setup()
  const onToggle = vi.fn()

  render(
    <TaskReplySection
      onCreate={vi.fn()}
      onImportReceipt={vi.fn()}
      onToggle={onToggle}
      open={false}
      replies={[]}
      sources={[source]}
      taskId="T-0001"
      unavailableSources={[{ ...source, display_title: 'Restricted Teams thread', provider: 'microsoft-teams' }]}
    />,
  )

  await user.click(screen.getByRole('button', { name: 'Prepare Outlook/Teams reply' }))
  expect(onToggle).toHaveBeenCalledOnce()
  expect(screen.getByText(/Restricted Teams thread cannot be used/)).toBeInTheDocument()
  expect(screen.getByText('Reply unavailable · Gate 0 pending')).toBeInTheDocument()
})

test('renders no reply controls without linked Microsoft sources', () => {
  const { container } = render(
    <TaskReplySection
      onCreate={vi.fn()}
      onImportReceipt={vi.fn()}
      onToggle={vi.fn()}
      open={false}
      replies={[]}
      sources={[]}
      taskId="T-0001"
      unavailableSources={[]}
    />,
  )

  expect(container).toBeEmptyDOMElement()
})
