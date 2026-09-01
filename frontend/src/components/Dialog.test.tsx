import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { Dialog } from './Dialog'
import { DialogLifecycleProvider } from './DialogLifecycle'

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
  const suspend = vi.fn(() => true)
  const resume = vi.fn()
  const lifecycle = { resume, suspend }
  const rendered = render(
    <DialogLifecycleProvider lifecycle={lifecycle}>
      <Dialog onClose={vi.fn()} open title="Review source">Contents</Dialog>
    </DialogLifecycleProvider>,
  )

  expect(suspend).toHaveBeenCalledOnce()
  rendered.rerender(
    <DialogLifecycleProvider lifecycle={lifecycle}>
      <Dialog onClose={vi.fn()} open={false} title="Review source">Contents</Dialog>
    </DialogLifecycleProvider>,
  )
  expect(resume).toHaveBeenCalledOnce()
})

test('is host agnostic when no lifecycle is provided', () => {
  expect(() => render(<Dialog onClose={vi.fn()} open title="Portable dialog">Contents</Dialog>)).not.toThrow()
})
