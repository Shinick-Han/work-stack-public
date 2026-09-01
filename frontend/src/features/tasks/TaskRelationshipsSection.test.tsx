import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { task } from '../../test/fixtures'
import { TaskRelationshipsSection } from './TaskRelationshipsSection'

test('labels each relationship direction and delegates Task navigation', async () => {
  const user = userEvent.setup()
  const onOpenTask = vi.fn()
  const parent = { ...task, id: 'T-0001', title: 'Parent Task' }
  const dependency = { ...task, id: 'T-0002', title: 'Dependency Task' }
  const child = { ...task, id: 'T-0003', title: 'Child Task' }
  const dependent = { ...task, id: 'T-0004', title: 'Dependent Task' }

  render(
    <TaskRelationshipsSection
      childTasks={[child]}
      dependentTasks={[dependent]}
      dependencyTasks={[dependency]}
      disabled={false}
      onOpenTask={onOpenTask}
      parentTask={parent}
    />,
  )

  for (const [label, id] of [
    ['Open parent T-0001', 'T-0001'],
    ['Open dependency T-0002', 'T-0002'],
    ['Open child T-0003', 'T-0003'],
    ['Open dependent T-0004', 'T-0004'],
  ] as const) {
    await user.click(screen.getByRole('button', { name: label }))
    expect(onOpenTask).toHaveBeenLastCalledWith(id)
  }
})

test('renders nothing when the Task has no relationships', () => {
  const { container } = render(
    <TaskRelationshipsSection
      childTasks={[]}
      dependentTasks={[]}
      dependencyTasks={[]}
      disabled={false}
      onOpenTask={vi.fn()}
      parentTask={null}
    />,
  )

  expect(container).toBeEmptyDOMElement()
})
