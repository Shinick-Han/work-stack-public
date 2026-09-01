import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { task, workspace } from '../../test/fixtures'
import { TaskOverviewEditor } from './TaskOverviewEditor'

test('delegates immediate overview edits to the drawer save coordinator', async () => {
  const user = userEvent.setup()
  const onDraftChange = vi.fn()
  const onSave = vi.fn()

  render(
    <TaskOverviewEditor
      availableDependencyTasks={[{ ...task, id: 'T-0002', title: 'Dependency Task' }]}
      availableParentTasks={[{ ...task, id: 'T-0003', title: 'Parent Task' }]}
      draft={task}
      isSaving={false}
      onDraftChange={onDraftChange}
      onMarkDirty={vi.fn()}
      onSave={onSave}
      onTagTextChange={vi.fn()}
      tagText="quality"
      workspace={workspace}
    />,
  )

  await user.selectOptions(screen.getByLabelText('Status'), 'done')
  expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'done' }))
  expect(onSave).toHaveBeenLastCalledWith({ status: 'done' })

  await user.selectOptions(screen.getByLabelText('Parent'), 'T-0003')
  expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ parent_id: 'T-0003' }))
  expect(onSave).toHaveBeenLastCalledWith({ parent_id: 'T-0003' })

  await user.click(screen.getByRole('checkbox'))
  expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ objective_ids: [] }))
  expect(onSave).toHaveBeenLastCalledWith({ objective_ids: [] })

  await user.selectOptions(screen.getByLabelText('Add dependency'), 'T-0002')
  expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ dependencies: ['T-0002'] }))
  expect(onSave).toHaveBeenLastCalledWith({ dependencies: ['T-0002'] })
})

test('delegates dirty field tracking without owning draft state', async () => {
  const user = userEvent.setup()
  const onDraftChange = vi.fn()
  const onMarkDirty = vi.fn()

  render(
    <TaskOverviewEditor
      availableDependencyTasks={[]}
      availableParentTasks={[]}
      draft={task}
      isSaving={false}
      onDraftChange={onDraftChange}
      onMarkDirty={onMarkDirty}
      onSave={vi.fn()}
      onTagTextChange={vi.fn()}
      tagText="quality"
      workspace={workspace}
    />,
  )

  await user.type(screen.getByLabelText('Definition of done'), ' updated')
  expect(onMarkDirty).toHaveBeenCalledWith('detail')
  expect(onDraftChange).toHaveBeenCalled()

  await user.type(screen.getByLabelText('Tags'), ', release')
  expect(onMarkDirty).toHaveBeenCalledWith('tags')
})
