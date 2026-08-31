import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { TreemapObjectiveNavigator } from './TreemapView'

describe('TreemapObjectiveNavigator', () => {
  it('focuses an aligned Objective without selecting a Task', async () => {
    const onSelectObjective = vi.fn()

    render(
      <TreemapObjectiveNavigator
        groups={[
          { name: 'O-1 · Release quality', objectiveId: 'O-1', children: [] },
          { name: 'Multiple objectives', objectiveId: 'multiple', children: [] },
          { name: 'Unaligned / Operations', objectiveId: 'none', children: [] },
        ]}
        onSelectObjective={onSelectObjective}
      />,
    )

    const group = screen.getByRole('button', { name: 'Focus objective O-1' })
    await userEvent.click(group)
    expect(onSelectObjective).toHaveBeenCalledWith('O-1')
    expect(screen.queryByRole('button', { name: 'Focus objective multiple' })).not.toBeInTheDocument()
  })

  it('keeps the unaligned operations group informational', () => {
    render(
      <TreemapObjectiveNavigator
        groups={[{ name: 'Unaligned / Operations', objectiveId: 'none', children: [] }]}
        onSelectObjective={vi.fn()}
      />,
    )

    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })
})
