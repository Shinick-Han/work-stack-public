import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'

import { GraphNotePopover } from './GraphNotePopover'

const NOTE = {
  id: 'N-0001',
  text: 'Full planning note body',
  created: '2026-09-01',
  links: ['T-0001', 'O-1'],
}

test('exposes the full note body and existing references without writes', () => {
  const onClose = vi.fn()
  render(<GraphNotePopover note={NOTE} onClose={onClose} />)

  const dialog = screen.getByRole('dialog', { name: NOTE.text })
  expect(dialog).toHaveTextContent('Full planning note body')
  expect(dialog).toHaveTextContent('T-0001')
  expect(dialog).toHaveTextContent('O-1')
  expect(screen.queryByRole('button', { name: 'Open task' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Close note' }))
  expect(onClose).toHaveBeenCalledTimes(1)
})

test('Escape closes the read-only note detail', () => {
  const onClose = vi.fn()
  render(<GraphNotePopover note={NOTE} onClose={onClose} />)
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(onClose).toHaveBeenCalledTimes(1)
})
