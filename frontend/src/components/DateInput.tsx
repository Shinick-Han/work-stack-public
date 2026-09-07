import { useEffect, useId, useLayoutEffect, useRef, useState, type InputHTMLAttributes } from 'react'
import { DateInputCalendar } from './DateInputCalendar'
import { clampDate, isDateAllowed, localToday, parseIsoDate } from './dateInputModel'
import './DateInput.css'

export const DATE_INPUT_CALENDAR_WIDTH_REM = 19
export const DATE_INPUT_CALENDAR_HEIGHT_REM = 21.5
export const DATE_INPUT_CALENDAR_GAP_PX = 4

export interface CalendarOverlayBox {
  top: number
  bottom: number
  left: number
}

export interface CalendarOverlayPlacement {
  top: number
  left: number
  width: number
  height: number
  placement: 'above' | 'below'
}

export function dateInputRootFontPx(): number {
  const rootFont = Number.parseFloat(getComputedStyle(document.documentElement).fontSize)
  return Number.isFinite(rootFont) && rootFont > 0 ? rootFont : 16
}

export function dateInputCalendarDesignedSize(rem = dateInputRootFontPx()): { width: number; height: number } {
  return {
    width: DATE_INPUT_CALENDAR_WIDTH_REM * rem,
    height: DATE_INPUT_CALENDAR_HEIGHT_REM * rem,
  }
}

/** Viewport-relative overlay box: prefer below, else above, then clamp the full box into the viewport. */
export function calendarOverlayPlacement(
  field: CalendarOverlayBox,
  overlay: { width: number; height: number },
  viewport: { width: number; height: number },
  gap = DATE_INPUT_CALENDAR_GAP_PX,
): CalendarOverlayPlacement {
  const width = Math.min(overlay.width, Math.max(0, viewport.width))
  const height = Math.min(overlay.height, Math.max(0, viewport.height))
  const left = Math.min(Math.max(0, field.left), Math.max(0, viewport.width - width))
  const spaceBelow = viewport.height - field.bottom
  const spaceAbove = field.top
  const placement: CalendarOverlayPlacement['placement'] = spaceBelow >= height + gap || spaceBelow >= spaceAbove ? 'below' : 'above'
  const unclampedTop = placement === 'below' ? field.bottom + gap : field.top - height - gap
  const top = Math.min(Math.max(0, unclampedTop), Math.max(0, viewport.height - height))
  return { top, left, width, height, placement }
}

function resetCalendarOverlayToDesigned(calendar: HTMLElement, designed: { width: number; height: number }) {
  calendar.style.width = `${designed.width}px`
  calendar.style.height = `${designed.height}px`
  calendar.style.maxWidth = `${designed.width}px`
  calendar.style.maxHeight = `${designed.height}px`
}

function applyCalendarOverlayBox(calendar: HTMLElement, box: CalendarOverlayPlacement) {
  calendar.style.position = 'fixed'
  calendar.style.top = `${box.top}px`
  calendar.style.left = `${box.left}px`
  calendar.style.width = `${box.width}px`
  calendar.style.height = `${box.height}px`
  calendar.style.maxWidth = `${box.width}px`
  calendar.style.maxHeight = `${box.height}px`
  calendar.style.right = 'auto'
  calendar.style.bottom = 'auto'
}

export function syncDateInputCalendarOverlay(
  field: HTMLElement,
  calendar: HTMLElement,
  viewport: { width: number; height: number } = { width: window.innerWidth, height: window.innerHeight },
): CalendarOverlayPlacement {
  const designed = dateInputCalendarDesignedSize()
  resetCalendarOverlayToDesigned(calendar, designed)
  const box = calendarOverlayPlacement(field.getBoundingClientRect(), designed, viewport)
  applyCalendarOverlayBox(calendar, box)
  return box
}

export function trackDateInputCalendarOverlay(field: HTMLElement, calendar: HTMLElement): () => void {
  const update = () => { syncDateInputCalendarOverlay(field, calendar) }
  update()
  window.addEventListener('resize', update)
  window.addEventListener('scroll', update, true)
  window.visualViewport?.addEventListener('resize', update)
  window.visualViewport?.addEventListener('scroll', update)
  return () => {
    window.removeEventListener('resize', update)
    window.removeEventListener('scroll', update, true)
    window.visualViewport?.removeEventListener('resize', update)
    window.visualViewport?.removeEventListener('scroll', update)
  }
}

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
  const fieldRef = useRef<HTMLDivElement>(null)
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
  const today = localToday()
  const locked = disabled || readOnly

  useEffect(() => { setDraft(value) }, [value, resetKey])
  useEffect(() => { setOpen(false) }, [resetKey])
  useEffect(() => {
    inputRef.current?.setCustomValidity(invalid ? 'Enter a valid date as YYYY-MM-DD.' : '')
  }, [invalid])
  useEffect(() => { if (disabled || readOnly) setOpen(false) }, [disabled, readOnly])
  useEffect(() => {
    if (calendarOpen) dayRefs.current.get(focusedDate)?.focus({ preventScroll: true })
  }, [calendarOpen, focusedDate])
  useLayoutEffect(() => {
    if (!calendarOpen || !fieldRef.current || !calendarRef.current) return
    return trackDateInputCalendarOverlay(fieldRef.current, calendarRef.current)
  }, [calendarOpen])

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

  return (
    <div
      className={`date-input-control${className ? ` ${className}` : ''}`}
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
      <div className="date-input-control__field" ref={fieldRef}>
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
        <DateInputCalendar
          calendarId={calendarId}
          monthId={monthId}
          calendarRef={calendarRef}
          dayRefs={dayRefs}
          focusedDate={focusedDate}
          onFocusedDateChange={setFocusedDate}
          selected={value}
          today={today}
          lower={lower}
          upper={upper}
          onChoose={choose}
          onClose={closeCalendar}
        />
      )}
    </div>
  )
}
