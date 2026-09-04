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

test('keeps the context control a sibling of the node action', () => {
  const onActivate = vi.fn()
  const onOpenContext = vi.fn()
  render(<GraphNodeFrame data={{ id: 'T-0001', kind: 'task', title: 'Release gate',
    eyebrow: 'T-0001', contextCount: 3, selected: false, related: true, onActivate, onOpenContext }} />)
  const badge = screen.getByRole('button', { name: /Open context for task T-0001/ })
  const node = screen.getByRole('button', { name: 'Open task T-0001' })
  expect(node).toHaveAttribute('tabindex', '0')
  expect(node.contains(badge)).toBe(false)
  fireEvent.click(badge)
  expect(onOpenContext).toHaveBeenCalledExactlyOnceWith(badge)
  expect(onActivate).not.toHaveBeenCalled()
})
