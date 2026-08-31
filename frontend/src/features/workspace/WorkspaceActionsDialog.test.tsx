import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { task, workspace } from '../../test/fixtures'
import { WorkspaceActionsDialog } from './WorkspaceActionsDialog'

test('keeps Objective creation in Objective Hub and exposes Context cards here', () => {
  render(
    <WorkspaceActionsDialog
      onClose={vi.fn()}
      onCreateNote={vi.fn(() => Promise.resolve())}
      open
      pending={false}
      workspace={workspace}
    />,
  )

  expect(screen.queryByRole('button', { name: 'Add objective' })).not.toBeInTheDocument()
  expect(screen.getByRole('textbox', { name: 'Context card' })).toBeVisible()
})

test('creates a Context card linked to selected workspace records', async () => {
  const onCreateNote = vi.fn(() => Promise.resolve())
  render(
    <WorkspaceActionsDialog
      onClose={vi.fn()}
      onCreateNote={onCreateNote}
      open
      pending={false}
      workspace={workspace}
    />,
  )

  const dialog = screen.getByRole('dialog', { name: 'Workspace actions' })
  await userEvent.type(within(dialog).getByRole('textbox', { name: 'Context card' }), 'Keep this decision visible')
  await userEvent.click(within(dialog).getByRole('checkbox', { name: new RegExp(`${task.id}.*${task.title}`, 'i') }))
  await userEvent.click(within(dialog).getByRole('button', { name: 'Add context card' }))

  expect(onCreateNote).toHaveBeenCalledWith(
    'Keep this decision visible',
    [task.id],
    expect.stringMatching(/^workstack:/),
  )
})

test('retains the same Context card intent key for an explicit unchanged retry', async () => {
  const onCreateNote = vi.fn()
    .mockRejectedValueOnce(new Error('Response lost'))
    .mockResolvedValueOnce(undefined)
  render(
    <WorkspaceActionsDialog
      onClose={vi.fn()}
      onCreateNote={onCreateNote}
      open
      pending={false}
      workspace={workspace}
    />,
  )

  await userEvent.type(screen.getByRole('textbox', { name: 'Context card' }), 'One logical intent')
  await userEvent.click(screen.getByRole('button', { name: 'Add context card' }))
  await screen.findByRole('alert')
  await userEvent.click(screen.getByRole('button', { name: 'Add context card' }))

  await waitFor(() => expect(onCreateNote).toHaveBeenCalledTimes(2))
  expect(onCreateNote.mock.calls[1][2]).toBe(onCreateNote.mock.calls[0][2])
})
