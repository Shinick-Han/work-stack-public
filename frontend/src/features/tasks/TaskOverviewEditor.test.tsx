import { useState } from 'react'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { task, workspace } from '../../test/fixtures'
import { TaskOverviewEditor } from './TaskOverviewEditor'
import type { EditableTaskPatch } from './taskDrawerModel'
import type { Task } from '../../domain/types'

function DateEditor({ onSave, isSaving = false }: { onSave: (patch: EditableTaskPatch) => void; isSaving?: boolean }) {
  const [draft, setDraft] = useState<Task>({ ...task, scheduled: '2024-02-28', due: '2024-02-28' })
  return <><TaskOverviewEditor availableDependencyTasks={[]} availableParentTasks={[]} draft={draft} isSaving={isSaving} onDraftChange={setDraft} onMarkDirty={vi.fn()} onSave={onSave} onTagTextChange={vi.fn()} tagText="" workspace={workspace} /><button>Outside editor</button></>
}

test('calendar selection saves the latest accepted planned date only on composite exit', async () => {
  const onSave = vi.fn()
  render(<DateEditor onSave={onSave} />)
  const planned = screen.getByRole('textbox', { name: 'Plan for' })
  const control = planned.closest('.date-input-control')! as HTMLElement
  await userEvent.click(planned)
  await userEvent.click(within(control).getByRole('button', { name: 'Choose date' }))
  await userEvent.click(screen.getByRole('button', { name: 'Next month' }))
  expect(onSave).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'March 2, 2024' }))
  expect(planned).toHaveValue('2024-03-02')
  expect(onSave).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Outside editor' }))
  expect(onSave).toHaveBeenCalledExactlyOnceWith({ scheduled: '2024-03-02' })
})

test('clear saves null on exit; an invalid due draft never enters the patch', async () => {
  const onSave = vi.fn()
  render(<DateEditor onSave={onSave} />)
  const due = screen.getByRole('textbox', { name: 'Due' })
  await userEvent.click(within(due.closest('.date-input-control')! as HTMLElement).getByRole('button', { name: 'Clear date' }))
  expect(onSave).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Outside editor' }))
  expect(onSave).toHaveBeenLastCalledWith({ due: null })
  onSave.mockClear()
  await userEvent.click(due)
  fireEvent.change(due, { target: { value: '2024-02-31' } })
  await userEvent.click(screen.getByRole('button', { name: 'Outside editor' }))
  expect(due).toHaveValue('2024-02-31')
  expect(onSave).not.toHaveBeenCalled()
})

test.each([['Due', 'due'], ['Plan for', 'scheduled']] as const)('%s replacement stays unsaved while invalid and saves only the repaired value', async (label, field) => {
  const onSave = vi.fn()
  render(<DateEditor onSave={onSave} />)
  const input = screen.getByRole('textbox', { name: label })
  expect(input).toHaveValue('2024-02-28')
  await userEvent.clear(input)
  await userEvent.type(input, '2024-02-31')
  expect(onSave).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('textbox', { name: 'Definition of done' }))
  expect(input).toHaveValue('2024-02-31')
  expect(input).toBeInvalid()
  expect(onSave).not.toHaveBeenCalled()
  // The ordinary detail field saves on its own blur; isolate the date's next exit.
  await userEvent.click(input)
  onSave.mockClear()
  await userEvent.clear(input)
  await userEvent.type(input, '2024-02-29')
  expect(onSave).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Outside editor' }))
  expect(onSave).toHaveBeenCalledExactlyOnceWith({ [field]: '2024-02-29' })
})

test.each([['Due', 'due'], ['Plan for', 'scheduled']] as const)('deleting %s to genuinely empty still saves null on exit', async (label, field) => {
  const onSave = vi.fn()
  render(<DateEditor onSave={onSave} />)
  await userEvent.clear(screen.getByRole('textbox', { name: label }))
  expect(onSave).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Outside editor' }))
  expect(onSave).toHaveBeenCalledExactlyOnceWith({ [field]: null })
})

test('saving disables both date controls including their calendar and clear actions', async () => {
  const onSave = vi.fn()
  render(<DateEditor onSave={onSave} isSaving />)
  for (const label of ['Plan for', 'Due']) {
    const input = screen.getByRole('textbox', { name: label })
    expect(input).toBeDisabled()
    const control = within(input.closest('.date-input-control')! as HTMLElement)
    expect(control.getByRole('button', { name: 'Choose date' })).toBeDisabled()
    expect(control.getByRole('button', { name: 'Clear date' })).toBeDisabled()
  }
  expect(onSave).not.toHaveBeenCalled()
})

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


const alignedWorkspace = {
  ...workspace,
  objectives: [{
    ...workspace.objectives[0],
    key_results: [{ id: 'KR-1', text: 'First outcome' }, { id: 'KR-2', text: 'Second outcome' }],
  }],
}

function AlignmentEditor({ initial, onSave }: { initial: Task; onSave: (patch: EditableTaskPatch) => void }) {
  const [draft, setDraft] = useState<Task>(initial)
  return <TaskOverviewEditor availableDependencyTasks={[]} availableParentTasks={[]} draft={draft} isSaving={false} onDraftChange={setDraft} onMarkDirty={vi.fn()} onSave={onSave} onTagTextChange={vi.fn()} tagText="" workspace={alignedWorkspace} />
}

test('the outcome editor is mounted in the Alignment area of the real overview editor', () => {
  render(<AlignmentEditor initial={{ ...task, objective_ids: ['O-1'] }} onSave={vi.fn()} />)

  const labels = screen.getAllByRole('option').map((option) => option.textContent)
  expect(screen.getByRole('combobox', { name: 'Add outcome' })).toBeEnabled()
  expect(labels).toContain('O-1 · KR-1 — First outcome')
  expect(labels).toContain('O-1 · KR-2 — Second outcome')
})

test('unchecking an Objective is blocked while its outcomes are retained, then allowed after removal', async () => {
  const onSave = vi.fn()
  const objectiveId = 'O-1'
  const keyResultId = 'KR-1'
  render(
    <AlignmentEditor
      initial={{
        ...task,
        objective_ids: [objectiveId],
        key_result_refs: [{ objective_id: objectiveId, key_result_id: keyResultId }],
      }}
      onSave={onSave}
    />,
  )

  const checkbox = screen.getAllByRole('checkbox')[0]
  expect(checkbox).toBeDisabled()
  expect(screen.getByText('Unlink its outcomes first')).toBeInTheDocument()
  expect(onSave).not.toHaveBeenCalled()

  await userEvent.click(screen.getByRole('button', { name: `Remove outcome ${objectiveId} ${keyResultId}` }))
  expect(onSave).toHaveBeenLastCalledWith({ key_result_refs: [] })

  await userEvent.click(screen.getAllByRole('checkbox')[0])
  expect(onSave).toHaveBeenLastCalledWith({ objective_ids: [] })
})
