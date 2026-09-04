import { useEffect, useId, useRef, useState, type InputHTMLAttributes, type KeyboardEvent } from 'react'
import { clampDate, dateLabel, isDateAllowed, localToday, monthCells, MONTH_NAMES, moveDay, moveMonth, parseIsoDate, weekday, WEEKDAY_NAMES } from './dateInputModel'
import './DateInput.css'

export interface DateInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'defaultValue' | 'onChange' | 'onBlur' | 'type' | 'min' | 'max'> {
  value: string
  onChange: (value: string) => void
  /** Publishes the valid date (or explicit empty value) on composite exit; invalid drafts never save. */
  onBlur?: (value: string) => void
  /** Change only for an explicit caller reset, including when value is still empty. */
  resetKey?: string | number
  label?: string
  min?: string
  max?: string
}

/** Controlled canonical value with a local, visibly invalid text draft. */
export function DateInput({ value, onChange, onBlur, resetKey, label, min, max, id, disabled, readOnly, className, onKeyDown, 'aria-describedby': describedBy, ...inputProps }: DateInputProps) {
  if ((min && !parseIsoDate(min)) || (max && !parseIsoDate(max)) || (min && max && min > max)) {
    throw new RangeError('DateInput bounds must be canonical dates with min <= max')
  }
  const generatedId = useId()
  const inputId = id ?? generatedId
  const calendarId = `${inputId}-calendar`
  const hintId = `${inputId}-hint`
  const monthId = `${inputId}-month`
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const calendarRef = useRef<HTMLDivElement>(null)
  const dayRefs = useRef(new Map<string, HTMLButtonElement>())
  const [draft, setDraft] = useState(value)
  const [open, setOpen] = useState(false)
  const [focusedDate, setFocusedDate] = useState(() => parseIsoDate(value) ? value : localToday())
  const lower = min && parseIsoDate(min) ? min : undefined
  const upper = max && parseIsoDate(max) ? max : undefined
  const invalid = draft !== '' && !isDateAllowed(draft, lower, upper)
  const calendarOpen = open && !disabled && !readOnly
  const currentMonth = parseIsoDate(focusedDate)!
  const today = localToday()
  const locked = disabled || readOnly

  useEffect(() => { setDraft(value) }, [value, resetKey])
  useEffect(() => { setOpen(false) }, [resetKey])
  useEffect(() => {
    inputRef.current?.setCustomValidity(invalid ? 'Enter a valid date as YYYY-MM-DD.' : '')
  }, [invalid])
  useEffect(() => { if (disabled || readOnly) setOpen(false) }, [disabled, readOnly])
  useEffect(() => {
    if (calendarOpen) {
      dayRefs.current.get(focusedDate)?.focus()
      calendarRef.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
    }
  }, [calendarOpen, focusedDate])

  useEffect(() => {
    if (!calendarOpen) return
    const closeOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) {
        // A click on a non-focusable outside surface must also finish composite editing.
        const active = document.activeElement
        if (active instanceof HTMLElement && rootRef.current?.contains(active)) active.blur()
        setOpen(false)
      }
    }
    document.addEventListener('pointerdown', closeOutside)
    return () => document.removeEventListener('pointerdown', closeOutside)
  }, [calendarOpen])

  function closeCalendar() {
    setOpen(false)
    inputRef.current?.focus()
  }

  function openCalendar() {
    if (locked) return
    const initial = isDateAllowed(draft, lower, upper) ? draft : clampDate(today, lower, upper)
    setFocusedDate(initial)
    setOpen(true)
  }

  function choose(next: string) {
    if (locked || (next !== '' && !isDateAllowed(next, lower, upper))) return
    setDraft(next)
    onChange(next)
    closeCalendar()
  }

  function navigateCalendar(event: KeyboardEvent<HTMLButtonElement>) {
    const offsets: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }
    let next: string | undefined
    if (event.key in offsets) next = moveDay(focusedDate, offsets[event.key])
    else if (event.key === 'PageUp' || event.key === 'PageDown') next = moveMonth(focusedDate, event.key === 'PageUp' ? -1 : 1)
    else if (event.key === 'Home') next = moveDay(focusedDate, -weekday(currentMonth))
    else if (event.key === 'End') next = moveDay(focusedDate, 6 - weekday(currentMonth))
    if (next) {
      event.preventDefault()
      event.stopPropagation()
      setFocusedDate(clampDate(next, lower, upper))
    }
  }

  function changeMonth(amount: number) {
    setFocusedDate(clampDate(moveMonth(focusedDate, amount), lower, upper))
  }

  return (
    <div
      className={`date-input-control${calendarOpen ? ' date-input-control--open' : ''}${className ? ` ${className}` : ''}`}
      ref={rootRef}
      onBlur={(event) => {
        if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return
        setOpen(false)
        if (!invalid) onBlur?.(draft)
      }}
      onKeyDown={(event) => {
        if (calendarOpen && event.key === 'Escape') {
          event.preventDefault()
          event.stopPropagation()
          closeCalendar()
        }
      }}
    >
      {label && <label htmlFor={inputId}>{label}</label>}
      <div className="date-input-control__field">
        <input
          {...inputProps}
          id={inputId}
          ref={inputRef}
          type="text"
          placeholder="YYYY-MM-DD"
          value={draft}
          disabled={disabled}
          readOnly={readOnly}
          aria-invalid={invalid || inputProps['aria-invalid'] || undefined}
          aria-describedby={[describedBy, hintId].filter(Boolean).join(' ')}
          onChange={(event) => {
            if (locked) return
            const next = event.target.value
            setDraft(next)
            if (next === '' || isDateAllowed(next, lower, upper)) onChange(next)
          }}
          onKeyDown={(event) => {
            onKeyDown?.(event)
            if (!event.defaultPrevented && !locked && event.key === 'ArrowDown') {
              event.preventDefault()
              event.stopPropagation()
              openCalendar()
            }
          }}
        />
        <button type="button" disabled={locked} aria-label="Choose date" aria-expanded={calendarOpen} aria-controls={calendarOpen ? calendarId : undefined} onClick={() => calendarOpen ? closeCalendar() : openCalendar()}>
          Calendar
        </button>
        <button type="button" disabled={locked || draft === ''} aria-label="Clear date" onClick={() => choose('')}>Clear</button>
      </div>
      <span id={hintId} className={invalid ? 'date-input-control__error' : 'date-input-control__hint'}>
        {invalid ? `Enter a valid date as YYYY-MM-DD${lower ? `, on or after ${lower}` : ''}${upper ? `, on or before ${upper}` : ''}.` : 'YYYY-MM-DD'}
      </span>
      {calendarOpen && (
        <div id={calendarId} ref={calendarRef} className="date-input-control__calendar" role="group" aria-label="Choose date calendar" lang="en">
          <div className="date-input-control__heading">
            <button type="button" aria-label="Previous month" disabled={clampDate(moveMonth(focusedDate, -1), lower, upper).slice(0, 7) === focusedDate.slice(0, 7)} onClick={() => changeMonth(-1)}>‹</button>
            <span id={monthId} aria-live="polite">{MONTH_NAMES[currentMonth.month - 1]} {String(currentMonth.year).padStart(4, '0')}</span>
            <button type="button" aria-label="Next month" disabled={clampDate(moveMonth(focusedDate, 1), lower, upper).slice(0, 7) === focusedDate.slice(0, 7)} onClick={() => changeMonth(1)}>›</button>
          </div>
          <table role="grid" aria-labelledby={monthId}>
            <thead><tr>{WEEKDAY_NAMES.map((day) => <th key={day} scope="col"><abbr title={day}>{day.slice(0, 3)}</abbr></th>)}</tr></thead>
            <tbody>{monthCells(focusedDate).map((week, index) => (
              <tr key={index}>{week.map((date, column) => (
                <td key={date ?? `empty-${column}`} aria-selected={date ? date === value : undefined}>
                  {date && <button
                    type="button"
                    ref={(element) => { if (element) dayRefs.current.set(date, element); else dayRefs.current.delete(date) }}
                    aria-label={dateLabel(date)}
                    aria-current={date === today ? 'date' : undefined}
                    tabIndex={date === focusedDate ? 0 : -1}
                    disabled={!isDateAllowed(date, lower, upper)}
                    onFocus={() => setFocusedDate(date)}
                    onKeyDown={navigateCalendar}
                    onClick={() => choose(date)}
                  >{Number(date.slice(-2))}</button>}
                </td>
              ))}</tr>
            ))}</tbody>
          </table>
          <div className="date-input-control__actions">
            <button type="button" disabled={!isDateAllowed(today, lower, upper)} onClick={() => choose(today)}>Today</button>
            <button type="button" onClick={closeCalendar}>Close calendar</button>
          </div>
        </div>
      )}
    </div>
  )
}
