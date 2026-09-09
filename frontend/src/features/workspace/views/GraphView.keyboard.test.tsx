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

test('clicking anywhere on the visual card activates exactly once', () => {
  const onActivate = vi.fn()
  const { container } = render(
    <GraphNodeFrame
      data={{
        id: 'KR-1',
        kind: 'key-result',
        title: 'Ship the gate',
        eyebrow: 'O-A · KR-1',
        contextCount: 0,
        selected: false,
        related: true,
        outcome: {
          objectiveId: 'O-A',
          keyResultId: 'KR-1',
          recordedProgress: null,
          target: null,
          status: null,
          linkedTotal: 0,
          visibleTotal: 0,
        },
        onActivate,
      }}
    />,
  )

  fireEvent.click(container.querySelector('.wsv-graph-node') as HTMLElement)
  fireEvent.click(container.querySelector('.wsv-graph-node__outcome') as HTMLElement)
  fireEvent.click(screen.getByRole('button', { name: 'Highlight key result O-A KR-1' }))
  expect(onActivate).toHaveBeenCalledTimes(3)
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

test('zero-context + Context is a sibling control and does not activate the node', () => {
  const onActivate = vi.fn()
  const onOpenContext = vi.fn()
  render(<GraphNodeFrame data={{ id: 'T-0002', kind: 'task', title: 'Release gate',
    eyebrow: 'T-0002', contextCount: 0, selected: false, related: true, onActivate, onOpenContext }} />)
  const add = screen.getByRole('button', { name: 'Add context for task T-0002' })
  expect(add).toHaveTextContent('+ Context')
  const node = screen.getByRole('button', { name: 'Open task T-0002' })
  expect(node.contains(add)).toBe(false)
  fireEvent.click(add)
  expect(onOpenContext).toHaveBeenCalledExactlyOnceWith(add)
  expect(onActivate).not.toHaveBeenCalled()
})

test('move handle Enter is distinct from card activation', () => {
  const onActivate = vi.fn()
  const onMoveKeyDown = vi.fn()
  render(
    <GraphNodeFrame
      data={{
        id: 'T-0001',
        kind: 'task',
        title: 'Release gate',
        eyebrow: 'T-0001',
        contextCount: 0,
        selected: false,
        related: true,
        onActivate,
        moveHandleLabel: 'Move task T-0001',
        onMoveKeyDown,
      }}
    />,
  )
  const handle = screen.getByRole('button', { name: 'Move task T-0001' })
  const card = screen.getByRole('button', { name: 'Open task T-0001' })
  fireEvent.keyDown(handle, { key: 'Enter' })
  fireEvent.keyDown(handle, { key: ' ' })
  expect(onMoveKeyDown).toHaveBeenCalledTimes(2)
  expect(onActivate).not.toHaveBeenCalled()
  fireEvent.keyDown(card, { key: 'Enter' })
  expect(onActivate).toHaveBeenCalledTimes(1)
})
