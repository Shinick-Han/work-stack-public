import { type KeyboardEvent, type RefObject } from 'react'
import { clampDate, dateLabel, isDateAllowed, monthCells, MONTH_NAMES, moveDay, moveMonth, parseIsoDate, weekday, WEEKDAY_NAMES } from './dateInputModel'

export interface DateInputCalendarProps {
  calendarId: string
  monthId: string
  calendarRef: RefObject<HTMLDivElement | null>
  dayRefs: RefObject<Map<string, HTMLButtonElement>>
  /** Roving-focus day; the caller owns it so it can restore focus after a re-render. */
  focusedDate: string
  onFocusedDateChange: (date: string) => void
  /** Canonical committed value, so a stale draft never marks a cell selected. */
  selected: string
  today: string
  lower?: string
  upper?: string
  onChoose: (date: string) => void
  onClose: () => void
}

/** Fixed-geometry overlay grid: the caller positions it and owns the open/close state. */
export function DateInputCalendar({
  calendarId,
  monthId,
  calendarRef,
  dayRefs,
  focusedDate,
  onFocusedDateChange,
  selected,
  today,
  lower,
  upper,
  onChoose,
  onClose,
}: DateInputCalendarProps) {
  const currentMonth = parseIsoDate(focusedDate)!
  const weeks = monthCells(focusedDate)
  // Always six rows so the overlay keeps one height across months.
  const overlayWeeks = weeks.concat(Array.from({ length: Math.max(0, 6 - weeks.length) }, () => Array<string | null>(7).fill(null)))

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
      onFocusedDateChange(clampDate(next, lower, upper))
    }
  }

  function changeMonth(amount: number) {
    onFocusedDateChange(clampDate(moveMonth(focusedDate, amount), lower, upper))
  }

  function monthStep(amount: number) {
    return clampDate(moveMonth(focusedDate, amount), lower, upper).slice(0, 7) === focusedDate.slice(0, 7)
  }

  return (
    <div id={calendarId} ref={calendarRef} className="date-input-control__calendar" role="group" aria-label="Choose date calendar" lang="en">
      <div className="date-input-control__heading">
        <button type="button" aria-label="Previous month" disabled={monthStep(-1)} onClick={() => changeMonth(-1)}>‹</button>
        <span id={monthId} aria-live="polite">{MONTH_NAMES[currentMonth.month - 1]} {String(currentMonth.year).padStart(4, '0')}</span>
        <button type="button" aria-label="Next month" disabled={monthStep(1)} onClick={() => changeMonth(1)}>›</button>
      </div>
      <div className="date-input-control__calendar-body">
        <table role="grid" aria-labelledby={monthId}>
          <thead><tr>{WEEKDAY_NAMES.map((day) => <th key={day} scope="col"><abbr title={day}>{day.slice(0, 3)}</abbr></th>)}</tr></thead>
          <tbody>{overlayWeeks.map((week, index) => (
            <tr key={index}>{week.map((date, column) => (
              <td key={date ?? `empty-${column}`} aria-selected={date ? date === selected : undefined}>
                {date && <button
                  type="button"
                  ref={(element) => { if (element) dayRefs.current.set(date, element); else dayRefs.current.delete(date) }}
                  aria-label={dateLabel(date)}
                  aria-current={date === today ? 'date' : undefined}
                  tabIndex={date === focusedDate ? 0 : -1}
                  disabled={!isDateAllowed(date, lower, upper)}
                  onFocus={() => onFocusedDateChange(date)}
                  onKeyDown={navigateCalendar}
                  onClick={() => onChoose(date)}
                >{Number(date.slice(-2))}</button>}
              </td>
            ))}</tr>
          ))}</tbody>
        </table>
      </div>
      <div className="date-input-control__actions">
        <button type="button" disabled={!isDateAllowed(today, lower, upper)} onClick={() => onChoose(today)}>Today</button>
        <button type="button" onClick={onClose}>Close calendar</button>
      </div>
    </div>
  )
}
