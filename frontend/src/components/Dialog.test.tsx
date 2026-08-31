import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { Dialog } from './Dialog'

const originalChrome = (window as Window & { chrome?: unknown }).chrome

afterEach(() => {
  Object.defineProperty(window, 'chrome', { configurable: true, value: originalChrome })
})

test('prevents native cancel before delegating close to the controlled parent', () => {
  const onClose = vi.fn()
  render(<Dialog onClose={onClose} open title="Controlled dialog">Contents</Dialog>)

  const dialog = screen.getByRole('dialog')
  const cancelEvent = new Event('cancel', { cancelable: true })
  fireEvent(dialog, cancelEvent)

  expect(cancelEvent.defaultPrevented).toBe(true)
  expect(onClose).toHaveBeenCalledOnce()
  expect(dialog).toHaveAttribute('open')
})

test('temporarily suspends the native Microsoft surface while a dialog is open', () => {
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: { postMessage } } })
  const rendered = render(<Dialog onClose={vi.fn()} open title="Review source">Contents</Dialog>)

  expect(postMessage).toHaveBeenCalledWith('workstack-source-host|suspend')
  rendered.rerender(<Dialog onClose={vi.fn()} open={false} title="Review source">Contents</Dialog>)
  expect(postMessage).toHaveBeenLastCalledWith('workstack-source-host|resume')
})
