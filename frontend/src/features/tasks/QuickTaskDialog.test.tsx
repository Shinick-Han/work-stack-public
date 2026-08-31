import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'
import { workspace } from '../../test/fixtures'
import { QuickTaskDialog } from './QuickTaskDialog'
import { QUICK_TASK_DRAFT_KEY, writeQuickTaskDraft } from './quickTaskDraft'

beforeEach(() => window.localStorage.clear())

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((finish) => { resolve = finish })
  return { promise, resolve }
}

test('sends a same-tick double submit once, locks every field, and unlocks after pending ends', async () => {
  const request = deferred()
  const onClose = vi.fn()
  const onSubmit = vi.fn(() => {
    void request.promise
  })
  const props = {
    error: null,
    onClose,
    onSubmit,
    open: true,
    workspace,
  }

  const { rerender } = render(<QuickTaskDialog {...props} pending={false} />)
  const user = userEvent.setup()
  const title = screen.getByLabelText('Task title')
  const detail = screen.getByLabelText(/Definition of done/)
  const priority = screen.getByLabelText('Priority')
  const due = screen.getByLabelText('Due')
  const objective = screen.getByLabelText(/Objective/)
  const tags = screen.getByLabelText(/Tags/)

  await user.type(title, 'Preserve this draft')
  await user.type(detail, 'Ship the result')
  await user.selectOptions(priority, 'P1')
  fireEvent.change(due, { target: { value: '2026-09-01' } })
  await user.selectOptions(objective, 'O-1')
  await user.type(tags, 'release, quality')

  const form = document.querySelector<HTMLFormElement>('#quick-task-form')!
  act(() => {
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  })

  expect(onSubmit).toHaveBeenCalledOnce()
  rerender(<QuickTaskDialog {...props} pending />)
  expect(title).toBeDisabled()
  for (const field of [detail, priority, due, objective, tags]) expect(field).toBeDisabled()

  const dialog = screen.getByRole('dialog')
  const cancelEvent = new Event('cancel', { cancelable: true })
  fireEvent(dialog, cancelEvent)
  expect(cancelEvent.defaultPrevented).toBe(true)
  expect(onClose).not.toHaveBeenCalled()
  expect(dialog).toHaveAttribute('open')
  expect(title).toHaveValue('Preserve this draft')

  request.resolve()
  await request.promise
  rerender(<QuickTaskDialog {...props} pending={false} />)
  await waitFor(() => expect(title).toBeEnabled())

  fireEvent.submit(form)
  expect(onSubmit).toHaveBeenCalledTimes(2)
})

test('closing preserves the local draft while resetting the synchronous submit gate', async () => {
  const onSubmit = vi.fn()
  const props = {
    error: null,
    onClose: vi.fn(),
    onSubmit,
    pending: false,
    workspace,
  }
  const { rerender } = render(<QuickTaskDialog {...props} open />)

  const title = screen.getByLabelText('Task title')
  await userEvent.type(title, 'First draft')
  fireEvent.submit(document.querySelector<HTMLFormElement>('#quick-task-form')!)
  expect(onSubmit).toHaveBeenCalledOnce()

  rerender(<QuickTaskDialog {...props} open={false} />)
  await waitFor(() => expect(window.localStorage.getItem(QUICK_TASK_DRAFT_KEY)).toContain('First draft'))
  rerender(<QuickTaskDialog {...props} open />)

  expect(title).toHaveValue('First draft')
  fireEvent.submit(document.querySelector<HTMLFormElement>('#quick-task-form')!)
  expect(onSubmit).toHaveBeenCalledTimes(2)
})

test('explicit clear and a successful parent reset remove the saved draft', async () => {
  const props = {
    error: null,
    onClose: vi.fn(),
    onSubmit: vi.fn(),
    open: true,
    pending: false,
    workspace,
  }
  const { rerender } = render(<QuickTaskDialog {...props} resetDraftToken={0} />)
  const title = screen.getByLabelText('Task title')
  await userEvent.type(title, 'Saved locally')
  await waitFor(() => expect(window.localStorage.getItem(QUICK_TASK_DRAFT_KEY)).toContain('Saved locally'))

  await userEvent.click(screen.getByRole('button', { name: 'Clear draft' }))
  expect(title).toHaveValue('')
  expect(window.localStorage.getItem(QUICK_TASK_DRAFT_KEY)).toBeNull()

  await userEvent.type(title, 'Created draft')
  rerender(<QuickTaskDialog {...props} resetDraftToken={1} />)
  await waitFor(() => expect(title).toHaveValue(''))
  expect(window.localStorage.getItem(QUICK_TASK_DRAFT_KEY)).toBeNull()
})

test('an explicit Objective entry point replaces only draft alignment', async () => {
  writeQuickTaskDraft({
    detail: 'Preserve the definition of done',
    due: '',
    scheduled: '',
    estimateMinutes: '',
    objectiveId: 'O-previous',
    priority: 'P1',
    tags: 'delivery',
    title: 'Preserve this draft',
  })
  const onSubmit = vi.fn()
  render(
    <QuickTaskDialog
      error={null}
      initialObjectiveId="O-1"
      onClose={vi.fn()}
      onSubmit={onSubmit}
      open
      pending={false}
      workspace={workspace}
    />,
  )

  await waitFor(() => expect(screen.getByLabelText(/Objective/)).toHaveValue('O-1'))
  expect(screen.getByLabelText('Task title')).toHaveValue('Preserve this draft')
  expect(screen.getByLabelText(/Definition of done/)).toHaveValue('Preserve the definition of done')

  await userEvent.click(screen.getByRole('button', { name: 'Create task' }))
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    objective_ids: ['O-1'],
    title: 'Preserve this draft',
  }))
})

test('applies documented shorthand only after an explicit action', async () => {
  const onSubmit = vi.fn()
  render(<QuickTaskDialog error={null} onClose={vi.fn()} onSubmit={onSubmit} open pending={false} workspace={workspace} />)
  await userEvent.type(
    screen.getByLabelText('Task title'),
    'Prepare release review !P1 @O-1 /plan 2026-09-02 /due 2026-09-04 #report /estimate 90m',
  )
  await userEvent.click(screen.getByRole('button', { name: 'Apply shorthand' }))
  expect(screen.getByLabelText('Task title')).toHaveValue('Prepare release review')
  expect(screen.getByLabelText('Priority')).toHaveValue('P1')
  expect(screen.getByLabelText('Plan for')).toHaveValue('2026-09-02')
  expect(screen.getByLabelText('Due')).toHaveValue('2026-09-04')
  expect(screen.getByLabelText(/Estimate/)).toHaveValue(90)
  expect(screen.getByLabelText(/Objective/)).toHaveValue('O-1')
  expect(screen.getByLabelText(/Tags/)).toHaveValue('report')
  await userEvent.click(screen.getByRole('button', { name: 'Create task' }))
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    scheduled: '2026-09-02', due: '2026-09-04', estimate_minutes: 90,
  }))
})
