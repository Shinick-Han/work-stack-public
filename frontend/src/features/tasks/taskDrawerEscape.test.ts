import { afterEach, expect, test } from 'vitest'
import { escapeReturnsToTask, hasOpenDialog, isEditableTarget } from './taskDrawerEscape'

function key(overrides: Partial<KeyboardEventInit> = {}) {
  return new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, ...overrides })
}

function dispatch(target: EventTarget, event: KeyboardEvent) {
  target.dispatchEvent(event)
  return event
}

function mount(html: string) {
  document.body.innerHTML = html
  return document.body.firstElementChild as HTMLElement
}

afterEach(() => { document.body.innerHTML = '' })

test('a bare Escape on the surface returns to the Task', () => {
  const surface = mount('<div tabindex="-1"></div>')
  expect(escapeReturnsToTask(dispatch(surface, key()))).toBe(true)
})

test('only Escape answers; other keys are left alone', () => {
  const surface = mount('<div tabindex="-1"></div>')
  expect(escapeReturnsToTask(dispatch(surface, key({ key: 'Enter' })))).toBe(false)
})

test('Escape inside an editable control belongs to the control', () => {
  const composer = mount('<textarea></textarea>')
  expect(escapeReturnsToTask(dispatch(composer, key()))).toBe(false)
  const input = mount('<input />')
  expect(escapeReturnsToTask(dispatch(input, key()))).toBe(false)
  const rich = mount('<div contenteditable="true"></div>')
  // jsdom does not implement isContentEditable from the attribute alone.
  Object.defineProperty(rich, 'isContentEditable', { configurable: true, value: true })
  expect(escapeReturnsToTask(dispatch(rich, key()))).toBe(false)
  expect(isEditableTarget(rich)).toBe(true)
  expect(isEditableTarget(null)).toBe(false)
})

test('an Escape a nearer handler already answered is not answered twice', () => {
  const surface = mount('<div tabindex="-1"></div>')
  const event = key()
  event.preventDefault()
  expect(escapeReturnsToTask(dispatch(surface, event))).toBe(false)
})

test('an open dialog above the subview keeps the Escape', () => {
  const surface = mount('<div><span tabindex="-1"></span><dialog open></dialog></div>')
  const origin = surface.querySelector('span') as HTMLElement
  expect(hasOpenDialog(document)).toBe(true)
  expect(escapeReturnsToTask(dispatch(origin, key()))).toBe(false)
  surface.querySelector('dialog')?.removeAttribute('open')
  expect(hasOpenDialog(document)).toBe(false)
  expect(escapeReturnsToTask(dispatch(origin, key()))).toBe(true)
})

test('an aria-modal dialog counts as open even without the native element', () => {
  const surface = mount('<div><span tabindex="-1"></span><div role="dialog" aria-modal="true"></div></div>')
  const origin = surface.querySelector('span') as HTMLElement
  expect(escapeReturnsToTask(dispatch(origin, key()))).toBe(false)
})

test('a composing or modified Escape is a different gesture', () => {
  const surface = mount('<div tabindex="-1"></div>')
  expect(escapeReturnsToTask(dispatch(surface, key({ isComposing: true })))).toBe(false)
  expect(escapeReturnsToTask(dispatch(surface, key({ keyCode: 229 })))).toBe(false)
  expect(escapeReturnsToTask(dispatch(surface, key({ shiftKey: true })))).toBe(false)
  expect(escapeReturnsToTask(dispatch(surface, key({ metaKey: true })))).toBe(false)
  expect(escapeReturnsToTask(dispatch(surface, key({ ctrlKey: true })))).toBe(false)
  expect(escapeReturnsToTask(dispatch(surface, key({ altKey: true })))).toBe(false)
})
