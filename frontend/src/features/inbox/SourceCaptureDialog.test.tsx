import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { workspace } from '../../test/fixtures'
import { SourceCaptureDialog } from './SourceCaptureDialog'

test('reopening a source resets an invalid empty-valued due draft and allows the next submission', async () => {
  const props = {
    onClose: vi.fn(), onSubmit: vi.fn(async () => {}), provider: 'outlook' as const, workspace,
    seed: { provider: 'outlook' as const, title: 'Reviewed source', text: 'Reviewed content', sourceUrl: '', capturedAt: '2024-02-28T12:00:00Z' },
  }
  const view = render(<SourceCaptureDialog {...props} open />)
  const due = screen.getByRole('textbox', { name: 'Due' })
  expect(due).toHaveValue('')
  await userEvent.type(due, '2024-02-31')
  await userEvent.type(screen.getByRole('textbox', { name: 'Task title' }), 'Blocked draft')
  await userEvent.click(screen.getByRole('button', { name: 'Create Task from source' }))
  expect(props.onSubmit).not.toHaveBeenCalled()
  expect(due).toBeInvalid()
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(props.onClose).toHaveBeenCalledOnce()
  view.rerender(<SourceCaptureDialog {...props} open={false} />)
  view.rerender(<SourceCaptureDialog {...props} open />)
  expect(screen.getByRole('textbox', { name: 'Due' })).toHaveValue('')
  expect(screen.getByRole('textbox', { name: 'Due' })).toBeValid()
  expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  await userEvent.type(screen.getByRole('textbox', { name: 'Task title' }), 'Next valid draft')
  await userEvent.click(screen.getByRole('button', { name: 'Create Task from source' }))
  expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ taskTitle: 'Next valid draft', due: null }))
})

test('source dialog date selection is explicit, nullable, pending-safe, and reset on reopen', async () => {
  let finish!: () => void
  const props = {
    onClose: vi.fn(), onSubmit: vi.fn(() => new Promise<void>((resolve) => { finish = resolve })),
    provider: 'outlook' as const, workspace,
    seed: { provider: 'outlook' as const, title: 'Reviewed source', text: 'Reviewed content', sourceUrl: '', capturedAt: '2024-02-28T12:00:00Z' },
  }
  const view = render(<SourceCaptureDialog {...props} open />)
  await userEvent.type(screen.getByRole('textbox', { name: 'Task title' }), 'Follow up')
  const due = screen.getByRole('textbox', { name: 'Due' })
  expect(due.closest('label')).toBeNull()
  fireEvent.change(due, { target: { value: '2024-02-28' } })
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  await userEvent.click(screen.getByRole('button', { name: 'February 29, 2024' }))
  expect(props.onSubmit).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Create Task from source' }))
  expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ due: '2024-02-29' }))
  expect(due).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Clear date' })).toBeDisabled()
  await act(async () => finish())
  view.rerender(<SourceCaptureDialog {...props} open={false} />)
  view.rerender(<SourceCaptureDialog {...props} open />)
  expect(screen.getByRole('textbox', { name: 'Due' })).toHaveValue('')
  await userEvent.type(screen.getByRole('textbox', { name: 'Task title' }), 'Another follow up')
  fireEvent.change(screen.getByRole('textbox', { name: 'Due' }), { target: { value: '2024-02-29' } })
  await userEvent.click(screen.getByRole('button', { name: 'Clear date' }))
  await userEvent.click(screen.getByRole('button', { name: 'Create Task from source' }))
  expect(props.onSubmit).toHaveBeenLastCalledWith(expect.objectContaining({ due: null }))
  await act(async () => finish())
})
