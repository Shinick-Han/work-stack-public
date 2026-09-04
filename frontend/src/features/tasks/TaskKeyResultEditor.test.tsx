import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import type { Objective, Task, TaskKeyResultRef } from '../../domain/types'
import { TaskKeyResultEditor } from './TaskKeyResultEditor'

function objective(id: string, keyResults: Objective['key_results']): Objective {
  return { id, objective: `${id} objective`, revision: 2, key_results: keyResults }
}

const FIRST = objective('O-0001', [
  { id: 'KR-1', text: 'First outcome', progress: 25 },
  { id: 'KR-2', text: 'Second outcome' },
])
const SECOND = objective('O-0002', [{ id: 'KR-1', text: 'Other outcome' }])

function baseTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'T-0001',
    uid: '00000000-0000-4000-8000-000000000001',
    title: 'Task',
    detail: '',
    status: 'open',
    priority: 'P2',
    due: null,
    tags: [],
    objective_ids: ['O-0001'],
    parent_id: null,
    dependencies: [],
    subtasks: [],
    notes: [],
    revision: 4,
    context_count: 0,
    ...overrides,
  }
}

function Harness({
  initial,
  objectives = [FIRST, SECOND],
  onSave,
  isSaving = false,
}: {
  initial: Task
  objectives?: Objective[]
  onSave: (patch: { key_result_refs: TaskKeyResultRef[] }) => void
  isSaving?: boolean
}) {
  const [draft, setDraft] = useState<Task>(initial)
  return (
    <TaskKeyResultEditor
      draft={draft}
      isSaving={isSaving}
      objectives={objectives}
      onDraftChange={setDraft}
      onSave={onSave}
    />
  )
}

test('selecting an outcome emits one full sorted set and nothing else', async () => {
  const onSave = vi.fn()
  render(<Harness initial={baseTask({ objective_ids: ['O-0001', 'O-0002'] })} onSave={onSave} />)

  await userEvent.selectOptions(
    screen.getByRole('combobox', { name: 'Add outcome' }),
    screen.getByRole('option', { name: /Other outcome/ }),
  )
  await userEvent.selectOptions(
    screen.getByRole('combobox', { name: 'Add outcome' }),
    screen.getByRole('option', { name: /Second outcome/ }),
  )

  expect(onSave).toHaveBeenNthCalledWith(1, {
    key_result_refs: [{ objective_id: 'O-0002', key_result_id: 'KR-1' }],
  })
  expect(onSave).toHaveBeenNthCalledWith(2, {
    key_result_refs: [
      { objective_id: 'O-0001', key_result_id: 'KR-2' },
      { objective_id: 'O-0002', key_result_id: 'KR-1' },
    ],
  })
  expect(onSave.mock.calls.every(([patch]) => Object.keys(patch).length === 1)).toBe(true)
})

test('only aligned Objectives offer outcomes, and identical local IDs stay separate', async () => {
  render(<Harness initial={baseTask()} onSave={vi.fn()} />)

  const labels = screen.getAllByRole('option').map((option) => option.textContent)

  expect(labels).toEqual([
    'Choose an outcome…',
    'O-0001 · KR-1 — First outcome',
    'O-0001 · KR-2 — Second outcome',
  ])
  expect(labels.join(' ')).not.toContain('Other outcome')
})

test('removal emits the remaining set and clear emits an explicit empty list', async () => {
  const onSave = vi.fn()
  render(
    <Harness
      initial={baseTask({
        objective_ids: ['O-0001'],
        key_result_refs: [
          { objective_id: 'O-0001', key_result_id: 'KR-1' },
          { objective_id: 'O-0001', key_result_id: 'KR-2' },
        ],
      })}
      onSave={onSave}
    />,
  )

  await userEvent.click(screen.getByRole('button', { name: 'Remove outcome O-0001 KR-1' }))
  expect(onSave).toHaveBeenLastCalledWith({
    key_result_refs: [{ objective_id: 'O-0001', key_result_id: 'KR-2' }],
  })

  await userEvent.click(screen.getByRole('button', { name: 'Clear outcomes' }))
  expect(onSave).toHaveBeenLastCalledWith({ key_result_refs: [] })
})

test('rendering a legacy omitted field emits nothing at all', () => {
  const onSave = vi.fn()

  render(<Harness initial={baseTask()} onSave={onSave} />)

  expect(onSave).not.toHaveBeenCalled()
  expect(screen.getByText('No outcomes linked')).toBeInTheDocument()
})

test('an unresolved pair is shown by ID and can be removed without being dropped on read', async () => {
  const onSave = vi.fn()
  render(
    <Harness
      initial={baseTask({ key_result_refs: [{ objective_id: 'O-0404', key_result_id: 'KR-7' }] })}
      onSave={onSave}
    />,
  )

  expect(screen.getByText('O-0404 · KR-7')).toBeInTheDocument()
  expect(screen.getByText('Unresolved outcome')).toBeInTheDocument()
  expect(onSave).not.toHaveBeenCalled()

  await userEvent.click(screen.getByRole('button', { name: 'Remove outcome O-0404 KR-7' }))
  expect(onSave).toHaveBeenCalledExactlyOnceWith({ key_result_refs: [] })
})

test('recorded progress is shown as recorded and no completion percentage appears', () => {
  render(
    <Harness
      initial={baseTask({ key_result_refs: [{ objective_id: 'O-0001', key_result_id: 'KR-1' }] })}
      onSave={vi.fn()}
    />,
  )

  expect(screen.getByText('Recorded progress 25')).toBeInTheDocument()
  expect(screen.queryByText(/%/)).toBeNull()
})

test('an unaligned Task explains what to do and offers no outcomes', () => {
  render(<Harness initial={baseTask({ objective_ids: [] })} onSave={vi.fn()} />)

  expect(screen.getByRole('combobox', { name: 'Add outcome' })).toBeDisabled()
  expect(
    screen.getByText('Align this Task with an Objective first to link its outcomes.'),
  ).toBeInTheDocument()
})

test('every control is disabled while a save is in flight', () => {
  render(
    <Harness
      initial={baseTask({ key_result_refs: [{ objective_id: 'O-0001', key_result_id: 'KR-1' }] })}
      isSaving
      onSave={vi.fn()}
    />,
  )

  expect(screen.getByRole('combobox', { name: 'Add outcome' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Remove outcome O-0001 KR-1' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Clear outcomes' })).toBeDisabled()
})


const COLLIDING_FIRST = objective('O-A::B', [{ id: 'KR-X', text: 'Delimiter left' }])
const COLLIDING_SECOND = objective('O-A', [{ id: 'B::KR-X', text: 'Delimiter right' }])

test('OE-03 two delimiter-colliding pairs are independently selectable', async () => {
  const onSave = vi.fn()
  render(
    <Harness
      initial={baseTask({ objective_ids: ['O-A::B', 'O-A'] })}
      objectives={[COLLIDING_FIRST, COLLIDING_SECOND]}
      onSave={onSave}
    />,
  )

  const values = screen.getAllByRole('option').map((option) => (option as HTMLOptionElement).value)
  expect(new Set(values).size).toBe(values.length)

  await userEvent.selectOptions(
    screen.getByRole('combobox', { name: 'Add outcome' }),
    screen.getByRole('option', { name: /Delimiter right/ }),
  )
  expect(onSave).toHaveBeenLastCalledWith({
    key_result_refs: [{ objective_id: 'O-A', key_result_id: 'B::KR-X' }],
  })

  await userEvent.selectOptions(
    screen.getByRole('combobox', { name: 'Add outcome' }),
    screen.getByRole('option', { name: /Delimiter left/ }),
  )
  expect(onSave).toHaveBeenLastCalledWith({
    key_result_refs: [
      { objective_id: 'O-A', key_result_id: 'B::KR-X' },
      { objective_id: 'O-A::B', key_result_id: 'KR-X' },
    ],
  })
})

test('OE-03 each colliding pair is removed independently', async () => {
  const onSave = vi.fn()
  render(
    <Harness
      initial={baseTask({
        objective_ids: ['O-A::B', 'O-A'],
        key_result_refs: [
          { objective_id: 'O-A', key_result_id: 'B::KR-X' },
          { objective_id: 'O-A::B', key_result_id: 'KR-X' },
        ],
      })}
      objectives={[COLLIDING_FIRST, COLLIDING_SECOND]}
      onSave={onSave}
    />,
  )

  expect(screen.getAllByRole('button', { name: /^Remove outcome/ })).toHaveLength(2)

  await userEvent.click(screen.getByRole('button', { name: 'Remove outcome O-A::B KR-X' }))

  expect(onSave).toHaveBeenCalledExactlyOnceWith({
    key_result_refs: [{ objective_id: 'O-A', key_result_id: 'B::KR-X' }],
  })
})
