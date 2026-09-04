import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { capture, task, workspace } from '../../test/fixtures'
import { verifiedMicrosoftProviderGates } from '../../test/providerGates'
import type { Capture } from '../../domain/types'
import { CaptureDrawer } from './CaptureDrawer'

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
