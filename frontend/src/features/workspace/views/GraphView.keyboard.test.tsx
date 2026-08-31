import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'

import { GraphNodeFrame } from './GraphView'

test('activates an actionable Graph node with Enter and Space', () => {
  const onActivate = vi.fn()
  render(
    <GraphNodeFrame
      data={{
        id: 'T-0001',
        kind: 'task',
        title: 'Release gate',
        eyebrow: 'T-0001',
        contextCount: 0,
        priority: 'P0',
        status: 'started',
        selected: false,
        related: true,
        onActivate,
      }}
    />,
  )

  const node = screen.getByRole('button', { name: 'Open task T-0001' })
  fireEvent.keyDown(node, { key: 'Enter' })
  fireEvent.keyDown(node, { key: ' ' })
  expect(onActivate).toHaveBeenCalledTimes(2)
})
