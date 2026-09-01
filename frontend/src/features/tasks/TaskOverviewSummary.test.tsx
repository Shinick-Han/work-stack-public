import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { task, workspace } from '../../test/fixtures'
import { TaskOverviewSummary } from './TaskOverviewSummary'

test('delegates title, Objective navigation, export, and save recovery actions', async () => {
  const user = userEvent.setup()
  const onDraftChange = vi.fn()
  const onMarkDirty = vi.fn()
  const onOpenObjective = vi.fn()
  const onSaveTitle = vi.fn()
  const onOpenSnapshot = vi.fn()
  const onRetry = vi.fn()
  const onDiscard = vi.fn()

  render(
    <TaskOverviewSummary
      canDiscard
      canRetry
      draft={{ ...task, title: '  Release gate  ' }}
      isSaving={false}
      navigationLocked={false}
      objectives={workspace.objectives}
      onDiscard={onDiscard}
      onDraftChange={onDraftChange}
      onInvalidTitle={vi.fn()}
      onMarkDirty={onMarkDirty}
      onOpenObjective={onOpenObjective}
      onOpenSnapshot={onOpenSnapshot}
      onRetry={onRetry}
      onSaveTitle={onSaveTitle}
      saveError="Revision conflict"
    />,
  )

  await user.type(screen.getByLabelText('Task title'), '!')
  expect(onMarkDirty).toHaveBeenCalledWith('title')
  expect(onDraftChange).toHaveBeenCalled()

  await user.tab()
  expect(onSaveTitle).toHaveBeenCalledWith('Release gate')

  await user.click(screen.getByRole('button', { name: 'Open objective O-1' }))
  expect(onOpenObjective).toHaveBeenCalledWith('O-1')
  await user.click(screen.getByRole('button', { name: 'Export to Conduit' }))
  expect(onOpenSnapshot).toHaveBeenCalledOnce()
  await user.click(screen.getByRole('button', { name: 'Retry save' }))
  expect(onRetry).toHaveBeenCalledOnce()
  await user.click(screen.getByRole('button', { name: 'Discard unsaved changes' }))
  expect(onDiscard).toHaveBeenCalledOnce()
})

test('rejects an empty title without issuing a save', async () => {
  const user = userEvent.setup()
  const onInvalidTitle = vi.fn()
  const onSaveTitle = vi.fn()

  render(
    <TaskOverviewSummary
      canDiscard={false}
      canRetry={false}
      draft={{ ...task, title: '   ' }}
      isSaving={false}
      navigationLocked={false}
      objectives={[]}
      onDiscard={vi.fn()}
      onDraftChange={vi.fn()}
      onInvalidTitle={onInvalidTitle}
      onMarkDirty={vi.fn()}
      onOpenSnapshot={vi.fn()}
      onRetry={vi.fn()}
      onSaveTitle={onSaveTitle}
      saveError={null}
    />,
  )

  await user.click(screen.getByLabelText('Task title'))
  await user.tab()
  expect(onInvalidTitle).toHaveBeenCalledOnce()
  expect(onSaveTitle).not.toHaveBeenCalled()
})
