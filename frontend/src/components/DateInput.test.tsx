import { useState } from 'react'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { DateInput, type DateInputProps } from './DateInput'
import { DialogLifecycleProvider } from './DialogLifecycle'

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
