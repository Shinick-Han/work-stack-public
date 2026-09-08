import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { useState } from 'react'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import {
  calendarOverlayPlacement,
  dateInputCalendarDesignedSize,
  DATE_INPUT_CALENDAR_GAP_PX,
  DATE_INPUT_CALENDAR_HEIGHT_REM,
  DATE_INPUT_CALENDAR_WIDTH_REM,
  DateInput,
  syncDateInputCalendarOverlay,
  type DateInputProps,
} from './DateInput'
import { DialogLifecycleProvider } from './DialogLifecycle'

const dateInputCss = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'DateInput.css'), 'utf8')

function Controlled({ value = '', onChange, ...props }: Partial<DateInputProps>) {
  const [date, setDate] = useState(value)
  return <DateInput label="Due date" {...props} value={date} onChange={(next) => { setDate(next); onChange?.(next) }} />
}

test('text entry keeps invalid and incomplete drafts visible without publishing them', async () => {
  const changed = vi.fn()
  render(<Controlled onChange={changed} />)
  const input = screen.getByRole('textbox', { name: 'Due date' })
  expect(input).toHaveAttribute('placeholder', 'YYYY-MM-DD')
  expect(input).toHaveAttribute('type', 'text')
  await userEvent.type(input, '2025-02-29')
  expect(input).toHaveValue('2025-02-29')
  expect(input).toHaveAttribute('aria-invalid', 'true')
  expect(input).toBeInvalid()
  expect(input).toHaveAccessibleDescription('Enter a valid date as YYYY-MM-DD.')
  expect(changed).not.toHaveBeenCalled()
  fireEvent.change(input, { target: { value: '2024-02-29' } })
  expect(changed).toHaveBeenLastCalledWith('2024-02-29')
  expect(input).not.toHaveAttribute('aria-invalid')
  expect(input).toBeValid()
  fireEvent.change(input, { target: { value: '2024-02-' } })
  expect(changed).toHaveBeenCalledTimes(1)
  expect(input).toHaveValue('2024-02-')
})

test('clear and keyboard deletion emit empty, and external updates replace a draft', async () => {
  const changed = vi.fn()
  const view = render(<DateInput label="Due date" value="2024-02-29" onChange={changed} />)
  const input = screen.getByRole('textbox')
  fireEvent.change(input, { target: { value: 'invalid' } })
  await userEvent.click(screen.getByRole('button', { name: 'Clear date' }))
  expect(changed).toHaveBeenLastCalledWith('')
  expect(input).toHaveValue('')
  expect(input).toHaveFocus()
  view.rerender(<DateInput label="Due date" value="2025-12-31" onChange={changed} />)
  expect(input).toHaveValue('2025-12-31')
  await userEvent.clear(input)
  expect(changed).toHaveBeenCalledTimes(2)
  expect(changed).toHaveBeenLastCalledWith('')
})

test('preserves input identity and accessible names/descriptions supplied by callers', () => {
  render(<><span id="date-label">Planned</span><span id="help">Optional planning date</span><DateInput id="planned" name="planned" aria-labelledby="date-label" aria-describedby="help" value="" onChange={vi.fn()} /></>)
  const input = screen.getByRole('textbox', { name: 'Planned' })
  expect(input).toHaveAttribute('id', 'planned')
  expect(input).toHaveAttribute('name', 'planned')
  expect(input).toHaveAccessibleDescription('Optional planning date YYYY-MM-DD')
})

test.each(['disabled', 'readOnly'] as const)('%s refuses text and calendar mutations', async (flag) => {
  const changed = vi.fn()
  render(<DateInput aria-label="Date" value="2024-02-29" onChange={changed} {...{ [flag]: true }} />)
  const input = screen.getByRole('textbox', { name: 'Date' })
  await userEvent.type(input, 'x')
  fireEvent.keyDown(input, { key: 'ArrowDown' })
  expect(input).toHaveValue('2024-02-29')
  expect(changed).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Choose date' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Clear date' })).toBeDisabled()
  expect(screen.queryByRole('grid')).not.toBeInTheDocument()
})

test('opens by keyboard, crosses a leap-month boundary, selects with Enter and restores focus', async () => {
  const changed = vi.fn()
  render(<Controlled value="2024-02-28" onChange={changed} />)
  const input = screen.getByRole('textbox')
  input.focus()
  await userEvent.keyboard('{ArrowDown}')
  expect(screen.getByRole('grid', { name: 'February 2024' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'February 28, 2024' })).toHaveFocus()
  await userEvent.keyboard('{ArrowRight}')
  expect(screen.getByRole('button', { name: 'February 29, 2024' })).toHaveFocus()
  await userEvent.keyboard('{ArrowRight}')
  expect(screen.getByRole('grid', { name: 'March 2024' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'March 1, 2024' })).toHaveFocus()
  await userEvent.keyboard('{Enter}')
  expect(changed).toHaveBeenLastCalledWith('2024-03-01')
  expect(input).toHaveValue('2024-03-01')
  expect(input).toHaveFocus()
  expect(screen.queryByRole('grid')).not.toBeInTheDocument()
})

test('Escape closes only the calendar without committing or reaching a parent key handler', async () => {
  const changed = vi.fn(), parentKey = vi.fn()
  render(<div onKeyDown={parentKey}><Controlled value="2024-12-31" onChange={changed} /></div>)
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  parentKey.mockClear()
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  expect(screen.getByRole('textbox')).toHaveFocus()
  expect(changed).not.toHaveBeenCalled()
  expect(parentKey).not.toHaveBeenCalled()
})

test('English pointer controls and week/month keys preserve civil dates', async () => {
  render(<Controlled value="2024-01-31" />)
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  await userEvent.click(screen.getByRole('button', { name: 'Next month' }))
  expect(screen.getByRole('button', { name: 'February 29, 2024' })).toHaveFocus()
  expect(within(screen.getByRole('grid')).getByText('Sun')).toHaveAttribute('title', 'Sunday')
  await userEvent.keyboard('{PageDown}{Home}')
  expect(screen.getByRole('button', { name: 'March 24, 2024' })).toHaveFocus()
  await userEvent.keyboard('{End}{ArrowUp}')
  expect(screen.getByRole('button', { name: 'March 23, 2024' })).toHaveFocus()
  await userEvent.click(screen.getByRole('button', { name: 'Previous month' }))
  await userEvent.click(screen.getByRole('button', { name: 'February 10, 2024' }))
  expect(screen.getByRole('textbox')).toHaveValue('2024-02-10')
})

test('Today emits the local civil date with fixed English labels', async () => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(2024, 11, 31, 23, 30))
  try {
    render(<Controlled />)
    await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
    expect(screen.getByRole('grid', { name: 'December 2024' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'December 31, 2024' })).toHaveAttribute('aria-current', 'date')
    await userEvent.click(screen.getByRole('button', { name: 'Today' }))
    expect(screen.getByRole('textbox')).toHaveValue('2024-12-31')
  } finally { vi.useRealTimers() }
})

test('range bounds reject typed dates and disable calendar days without wrapping keyboard focus', async () => {
  const changed = vi.fn()
  render(<Controlled value="2024-02-29" min="2024-02-28" max="2024-03-01" onChange={changed} />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '2024-03-02' } })
  expect(changed).not.toHaveBeenCalled()
  expect(screen.getByRole('textbox')).toHaveAttribute('aria-invalid', 'true')
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '2024-02-29' } })
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  expect(screen.getByRole('button', { name: 'February 27, 2024' })).toBeDisabled()
  await userEvent.keyboard('{ArrowLeft}{ArrowLeft}')
  expect(screen.getByRole('button', { name: 'February 28, 2024' })).toHaveFocus()
  await userEvent.keyboard('{ArrowDown}{ArrowRight}')
  expect(screen.getByRole('button', { name: 'March 1, 2024' })).toHaveFocus()
})

test('malformed or reversed bounds cannot silently remove a caller constraint', () => {
  expect(() => render(<DateInput value="" min="2024-02-30" onChange={vi.fn()} />)).toThrow(RangeError)
  expect(() => render(<DateInput value="" min="2024-03-01" max="2024-02-29" onChange={vi.fn()} />)).toThrow(RangeError)
})

test('calendar stops at supported civil-year boundaries without generating an invalid year', async () => {
  const view = render(<Controlled value="0001-01-01" />)
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  expect(screen.getByRole('button', { name: 'Previous month' })).toBeDisabled()
  await userEvent.keyboard('{ArrowLeft}')
  expect(screen.getByRole('button', { name: 'January 1, 0001' })).toHaveFocus()
  view.unmount()
  render(<Controlled value="9999-12-31" />)
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  expect(screen.getByRole('button', { name: 'Next month' })).toBeDisabled()
  await userEvent.keyboard('{ArrowRight}{Enter}')
  expect(screen.getByRole('textbox')).toHaveValue('9999-12-31')
})

test('leaving an invalid draft does not normalize it or publish a replacement date', async () => {
  const changed = vi.fn(), blurred = vi.fn()
  render(<><Controlled value="2024-02-29" onChange={changed} onBlur={blurred} /><button>Outside</button></>)
  const input = screen.getByRole('textbox')
  await userEvent.click(input)
  fireEvent.change(input, { target: { value: '2024-02-31' } })
  await userEvent.click(screen.getByRole('button', { name: 'Outside' }))
  expect(input).toHaveValue('2024-02-31')
  expect(input).toHaveAttribute('aria-invalid', 'true')
  expect(changed).not.toHaveBeenCalled()
  expect(blurred).not.toHaveBeenCalled()
})

test('an explicit reset clears invalid text, calendar and validity even when the controlled value stays empty', async () => {
  const changed = vi.fn(), blurred = vi.fn()
  const props = { label: 'Due date', value: '', onChange: changed, onBlur: blurred }
  const view = render(<DateInput {...props} resetKey={0} />)
  const input = screen.getByRole('textbox', { name: 'Due date' })
  await userEvent.type(input, '2024-02-31')
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  view.rerender(<DateInput {...props} resetKey={0} />)
  expect(input).toHaveValue('2024-02-31')
  expect(input).toBeInvalid()
  expect(screen.getByRole('grid')).toBeInTheDocument()
  view.rerender(<DateInput {...props} resetKey={1} />)
  expect(input).toHaveValue('')
  expect(input).toBeValid()
  expect(input).not.toHaveAttribute('aria-invalid')
  expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  expect(changed).not.toHaveBeenCalled()
  expect(blurred).not.toHaveBeenCalled()
})

test('blur-save runs once on leaving the complete control, after calendar changes', async () => {
  const order: string[] = []
  render(<><Controlled value="2024-02-28" onChange={(date) => order.push(`change:${date}`)} onBlur={() => order.push('blur')} /><button>Outside</button></>)
  await userEvent.click(screen.getByRole('textbox'))
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  expect(order).toEqual([])
  await userEvent.click(screen.getByRole('button', { name: 'February 29, 2024' }))
  expect(order).toEqual(['change:2024-02-29'])
  await userEvent.click(screen.getByRole('button', { name: 'Outside' }))
  expect(order).toEqual(['change:2024-02-29', 'blur'])
})

test('Tab exits the calendar normally and closes it; outside pointer closes without stealing focus', async () => {
  const blurred = vi.fn()
  render(<><Controlled value="2024-02-29" onBlur={blurred} /><button>Outside</button></>)
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Today' })).toHaveFocus()
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Close calendar' })).toHaveFocus()
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Outside' })).toHaveFocus()
  expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  expect(blurred).toHaveBeenCalledOnce()
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  await userEvent.click(screen.getByRole('button', { name: 'Outside' }))
  expect(screen.getByRole('button', { name: 'Outside' })).toHaveFocus()
  expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  expect(blurred).toHaveBeenCalledTimes(2)
})

test('uses a contained calendar without changing parent dialog or Microsoft lifecycle', async () => {
  const lifecycle = { suspend: vi.fn(() => true), resume: vi.fn() }
  const { container } = render(<DialogLifecycleProvider lifecycle={lifecycle}><Controlled value="2024-02-29" /></DialogLifecycleProvider>)
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  expect(screen.getByRole('group', { name: 'Choose date calendar' })).toHaveAttribute('lang', 'en')
  expect(container.querySelector('dialog')).toBeNull()
  expect(lifecycle.suspend).not.toHaveBeenCalled()
  expect(lifecycle.resume).not.toHaveBeenCalled()
})

test('opening the calendar does not apply an open-state layout-span class', async () => {
  expect(dateInputCss).not.toContain('date-input-control--open')
  const { container } = render(
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr' }}>
      <Controlled value="2024-02-29" />
      <button type="button">Neighbor</button>
    </div>,
  )
  const root = container.querySelector('.date-input-control')
  expect(root).toBeTruthy()
  const closedClass = root!.className
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  expect(root!.className).toBe(closedClass)
  expect(root!.className.split(/\s+/)).not.toContain('date-input-control--open')
  expect(screen.getByRole('button', { name: 'Neighbor' })).toBeInTheDocument()
})

test('calendar is a fixed overlay that stays in the control and keeps six-week dimensions across months', async () => {
  expect(dateInputCss).toMatch(/height:\s*21\.5rem/)
  const { container } = render(<Controlled value="2024-02-01" />)
  const root = container.querySelector('.date-input-control') as HTMLElement
  await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
  const calendar = screen.getByRole('group', { name: 'Choose date calendar' })
  expect(calendar).toHaveClass('date-input-control__calendar')
  expect(calendar.parentElement).toBe(root)
  expect(calendar).toHaveStyle({ position: 'fixed' })
  expect(getComputedStyle(calendar).position).toBe('fixed')
  const bodyRows = () => within(screen.getByRole('grid')).getAllByRole('row').slice(1)
  expect(bodyRows()).toHaveLength(6)
  const openClass = root.className
  await userEvent.click(screen.getByRole('button', { name: 'Next month' }))
  expect(screen.getByRole('grid', { name: 'March 2024' })).toBeInTheDocument()
  expect(bodyRows()).toHaveLength(6)
  expect(root.className).toBe(openClass)
  expect(calendar).toHaveClass('date-input-control__calendar')
  expect(calendar).toHaveStyle({ position: 'fixed' })
})

test('opening focuses the current day without scrolling the page', async () => {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
  const scrollIntoView = vi.fn()
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView, writable: true })
  try {
    render(<Controlled value="2024-02-29" />)
    await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
    expect(screen.getByRole('button', { name: 'February 29, 2024' })).toHaveFocus()
    expect(scrollIntoView).not.toHaveBeenCalled()
    await userEvent.keyboard('{Escape}')
    expect(screen.getByRole('textbox', { name: 'Due date' })).toHaveFocus()
    expect(screen.queryByRole('grid')).not.toBeInTheDocument()
  } finally {
    if (original) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', original)
    else delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
  }
  expect(Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')).toEqual(original)
})

test.each([
  {
    name: 'places below when the field has room underneath',
    field: { top: 40, bottom: 72, left: 24 },
    overlay: { width: 304, height: 344 },
    viewport: { width: 1000, height: 900 },
    expected: { placement: 'below' as const, top: 72 + DATE_INPUT_CALENDAR_GAP_PX, left: 24, width: 304, height: 344 },
  },
  {
    name: 'places above when more space exists above the field',
    field: { top: 500, bottom: 532, left: 24 },
    overlay: { width: 304, height: 344 },
    viewport: { width: 800, height: 560 },
    expected: { placement: 'above' as const, top: 500 - 344 - DATE_INPUT_CALENDAR_GAP_PX, left: 24, width: 304, height: 344 },
  },
  {
    name: 'clamps left and width inside a narrow viewport',
    field: { top: 10, bottom: 42, left: 80 },
    overlay: { width: 304, height: 344 },
    viewport: { width: 200, height: 900 },
    expected: { placement: 'below' as const, top: 42 + DATE_INPUT_CALENDAR_GAP_PX, left: 0, width: 200, height: 344 },
  },
  {
    name: 'caps height to the viewport so a taller overlay stays on-screen',
    field: { top: 10, bottom: 42, left: 16 },
    overlay: { width: 304, height: 500 },
    viewport: { width: 800, height: 400 },
    expected: { placement: 'below' as const, top: 0, left: 16, width: 304, height: 400 },
  },
  {
    name: 'caps a 21.5rem calendar into a 320px viewport',
    field: { top: 40, bottom: 72, left: 16 },
    overlay: { width: 304, height: 344 },
    viewport: { width: 400, height: 320 },
    expected: { placement: 'below' as const, top: 0, left: 16, width: 304, height: 320 },
  },
  {
    name: 'shifts left so a right-aligned field does not overflow',
    field: { top: 20, bottom: 52, left: 900 },
    overlay: { width: 304, height: 344 },
    viewport: { width: 1000, height: 800 },
    expected: { placement: 'below' as const, top: 52 + DATE_INPUT_CALENDAR_GAP_PX, left: 696, width: 304, height: 344 },
  },
])('calendarOverlayPlacement $name', ({ field, overlay, viewport, expected }) => {
  const box = calendarOverlayPlacement(field, overlay, viewport)
  expect(box).toEqual(expected)
  expect(box.top).toBeGreaterThanOrEqual(0)
  expect(box.left).toBeGreaterThanOrEqual(0)
  expect(box.height).toBeLessThanOrEqual(viewport.height)
  expect(box.width).toBeLessThanOrEqual(viewport.width)
  expect(box.top + box.height).toBeLessThanOrEqual(viewport.height)
  expect(box.left + box.width).toBeLessThanOrEqual(viewport.width)
})

function viewportRect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON() { return this },
  } as DOMRect
}

test('open calendar writes clamped fixed coordinates from the field and recomputes on resize and scroll', async () => {
  const innerWidth = vi.spyOn(window, 'innerWidth', 'get')
  const innerHeight = vi.spyOn(window, 'innerHeight', 'get')
  innerWidth.mockReturnValue(1000)
  innerHeight.mockReturnValue(800)
  const overlay = {
    width: DATE_INPUT_CALENDAR_WIDTH_REM * 16,
    height: DATE_INPUT_CALENDAR_HEIGHT_REM * 16,
  }
  try {
    const { container } = render(<Controlled value="2024-02-29" />)
    const field = container.querySelector('.date-input-control__field') as HTMLElement
    const fieldRect = vi.spyOn(field, 'getBoundingClientRect')
    fieldRect.mockReturnValue(viewportRect(40, 400, 220, 32))
    await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
    const calendar = screen.getByRole('group', { name: 'Choose date calendar' })
    const expectedBelow = calendarOverlayPlacement(field.getBoundingClientRect(), overlay, { width: 1000, height: 800 })
    expect(expectedBelow.placement).toBe('below')
    expect(calendar.style.position).toBe('fixed')
    expect(calendar.style.top).toBe(`${expectedBelow.top}px`)
    expect(calendar.style.left).toBe(`${expectedBelow.left}px`)
    expect(calendar.style.width).toBe(`${expectedBelow.width}px`)
    expect(calendar.style.height).toBe(`${expectedBelow.height}px`)
    expect(calendar.style.maxHeight).toBe(`${expectedBelow.height}px`)
    innerHeight.mockReturnValue(520)
    fireEvent(window, new Event('resize'))
    const expectedAbove = calendarOverlayPlacement(field.getBoundingClientRect(), overlay, { width: 1000, height: 520 })
    expect(expectedAbove.placement).toBe('above')
    expect(calendar.style.top).toBe(`${expectedAbove.top}px`)
    fieldRect.mockReturnValue(viewportRect(40, 24, 220, 32))
    fireEvent(window, new Event('scroll'))
    const expectedAfterScroll = calendarOverlayPlacement(field.getBoundingClientRect(), overlay, { width: 1000, height: 520 })
    expect(expectedAfterScroll.placement).toBe('below')
    expect(calendar.style.top).toBe(`${expectedAfterScroll.top}px`)
    expect(calendar.style.left).toBe(`${expectedAfterScroll.left}px`)
    expect(calendar.parentElement).toBe(container.querySelector('.date-input-control'))
  } finally {
    innerWidth.mockRestore()
    innerHeight.mockRestore()
  }
})

test('syncDateInputCalendarOverlay caps height to 320px and expands width after a narrow clamp', () => {
  const field = document.createElement('div')
  const calendar = document.createElement('div')
  document.body.append(field, calendar)
  const designed = dateInputCalendarDesignedSize()
  vi.spyOn(field, 'getBoundingClientRect').mockReturnValue(viewportRect(16, 40, 180, 32))
  calendar.style.width = '200px'
  calendar.style.height = `${designed.height}px`
  Object.defineProperty(calendar, 'offsetWidth', { configurable: true, get: () => Number.parseFloat(calendar.style.width) || 0 })
  Object.defineProperty(calendar, 'offsetHeight', { configurable: true, get: () => Number.parseFloat(calendar.style.height) || 0 })
  try {
    const short = syncDateInputCalendarOverlay(field, calendar, { width: 400, height: 320 })
    expect(short.height).toBe(320)
    expect(short.top).toBeGreaterThanOrEqual(0)
    expect(short.top + short.height).toBeLessThanOrEqual(320)
    expect(short.left + short.width).toBeLessThanOrEqual(400)
    expect(calendar.style.height).toBe('320px')
    expect(calendar.style.maxHeight).toBe('320px')
    const narrow = syncDateInputCalendarOverlay(field, calendar, { width: 200, height: 800 })
    expect(narrow.width).toBe(200)
    expect(calendar.style.width).toBe('200px')
    expect(Number.parseFloat(calendar.style.width)).toBe(calendar.offsetWidth)
    const wide = syncDateInputCalendarOverlay(field, calendar, { width: 1000, height: 800 })
    expect(wide.width).toBe(designed.width)
    expect(wide.height).toBe(designed.height)
    expect(calendar.style.width).toBe(`${designed.width}px`)
    expect(calendar.style.maxWidth).toBe(`${designed.width}px`)
    expect(calendar.style.height).toBe(`${designed.height}px`)
    expect(wide.top + wide.height).toBeLessThanOrEqual(800)
  } finally {
    field.remove()
    calendar.remove()
  }
})

test('short viewport keeps Close outside the scrolling grid and a later wide resize restores 19rem', async () => {
  const innerWidth = vi.spyOn(window, 'innerWidth', 'get')
  const innerHeight = vi.spyOn(window, 'innerHeight', 'get')
  innerWidth.mockReturnValue(400)
  innerHeight.mockReturnValue(320)
  const designed = dateInputCalendarDesignedSize()
  try {
    const { container } = render(<Controlled value="2024-02-29" />)
    const field = container.querySelector('.date-input-control__field') as HTMLElement
    const fieldRect = vi.spyOn(field, 'getBoundingClientRect')
    fieldRect.mockReturnValue(viewportRect(16, 40, 180, 32))
    await userEvent.click(screen.getByRole('button', { name: 'Choose date' }))
    const calendar = screen.getByRole('group', { name: 'Choose date calendar' })
    const short = calendarOverlayPlacement(field.getBoundingClientRect(), designed, { width: 400, height: 320 })
    expect(short.height).toBe(320)
    expect(short.top + short.height).toBeLessThanOrEqual(320)
    expect(calendar.style.height).toBe('320px')
    expect(calendar.style.maxHeight).toBe('320px')
    const close = screen.getByRole('button', { name: 'Close calendar' })
    const actions = calendar.querySelector('.date-input-control__actions') as HTMLElement
    const body = calendar.querySelector('.date-input-control__calendar-body') as HTMLElement
    expect(actions).toBeTruthy()
    expect(body).toBeTruthy()
    expect(actions.contains(close)).toBe(true)
    expect(body.contains(close)).toBe(false)
    expect(body.contains(screen.getByRole('grid'))).toBe(true)
    expect(getComputedStyle(calendar).overflow).toMatch(/hidden/)
    expect(body.className).toBe('date-input-control__calendar-body')
    expect(within(calendar).getAllByRole('row').slice(1)).toHaveLength(6)
    innerWidth.mockReturnValue(200)
    innerHeight.mockReturnValue(800)
    fireEvent(window, new Event('resize'))
    expect(calendar.style.width).toBe('200px')
    innerWidth.mockReturnValue(1000)
    fireEvent(window, new Event('resize'))
    expect(calendar.style.width).toBe(`${designed.width}px`)
    expect(calendar.style.height).toBe(`${designed.height}px`)
  } finally {
    innerWidth.mockRestore()
    innerHeight.mockRestore()
  }
})

test('DateInput typography uses shared control tokens without changing the calendar footprint', () => {
  expect(dateInputCss).toMatch(/\.date-input-control__field input \{[^}]*font-size: var\(--ws-type-reading-control\)/)
  expect(dateInputCss).toMatch(/\.date-input-control__field input \{[^}]*min-height: var\(--ws-control-min-height\)/)
  expect(dateInputCss).toMatch(/\.date-input-control > label \{[^}]*font-size: var\(--ws-type-reading-control\)/)
  expect(dateInputCss).toMatch(/\.date-input-control\.date-input-control--property > label \{[^}]*font-size: var\(--ws-type-reading-label\)/)
  expect(dateInputCss).toMatch(/\.date-input-control--property \.date-input-control__field input \{[^}]*font-size: var\(--ws-type-reading-control\)/)
  expect(dateInputCss).toMatch(/\.date-input-control button \{[^}]*min-height: var\(--ws-control-min-height\)/)
  expect(dateInputCss).toMatch(/\.date-input-control__calendar \{[^}]*height: 21\.5rem/)
  expect(dateInputCss).toMatch(/\.date-input-control__calendar \{[^}]*width: 19rem/)
  expect(DATE_INPUT_CALENDAR_WIDTH_REM).toBe(19)
  expect(DATE_INPUT_CALENDAR_HEIGHT_REM).toBe(21.5)
  expect(dateInputCss).not.toMatch(/font-size:\s*1[01]px/)
})
