import { useId } from 'react'
import { Pill } from '../../components/Primitives'
import type { DailyReportContextCatalog } from '../../domain/reporting'
import { formatDateTime } from '../../utils/format'

export const CONTEXT_PANEL_TITLE = 'Related task context'
export const CONTEXT_PANEL_COPY =
  "A snapshot of context linked to this report's tasks when the preview was generated."
  + ' It is not included in the report and does not verify individual claims.'
export const CONTEXT_PANEL_EMPTY = 'No context is linked to these tasks.'

export function omittedContextMessage(count: number): string {
  const subject = count === 1 ? '1 more linked capture is' : `${count} more linked captures are`
  return `${subject} not shown here. Review Task context for the full list.`
}

/**
 * A read-only sibling of the generated daily preview. It renders decoded
 * catalogue metadata as inert React text only: no link, open, refresh, verify
 * or mutation control, and no request of its own. The owning preview decides
 * when this catalogue exists, so an invalidated day simply stops rendering it.
 * The weekly preview reuses this same component beside its own owned preview.
 */
export function DailyReportContextPanel({ catalog }: { catalog: DailyReportContextCatalog }) {
  // Per instance: the daily and weekly previews can be mounted together, so a
  // fixed heading id would be duplicated across the two panels.
  const headingId = useId()
  return (
    <section className="daily-report-context" aria-labelledby={headingId}>
      <h3 className="daily-report-context__title" id={headingId}>
        {CONTEXT_PANEL_TITLE}
      </h3>
      <p className="daily-report-context__copy">{CONTEXT_PANEL_COPY}</p>
      <p className="daily-report-context__meta">
        Captured {formatDateTime(catalog.captured_at)}
      </p>
      {catalog.items.length === 0 ? (
        <p className="daily-report-context__empty">{CONTEXT_PANEL_EMPTY}</p>
      ) : (
        <ul className="daily-report-context__list">
          {catalog.items.map((item) => (
            <li className="daily-report-context__item" key={item.capture_id}>
              <span className="daily-report-context__identity">
                <strong>{item.capture_id}</strong>
                <span className="daily-report-context__item-title">{item.title}</span>
                <Pill tone={item.status}>{item.status}</Pill>
              </span>
              <span className="daily-report-context__tasks">
                Linked tasks: {item.linked_task_ids.join(', ')}
              </span>
            </li>
          ))}
        </ul>
      )}
      {catalog.omitted_count > 0 ? (
        <p className="daily-report-context__omitted">{omittedContextMessage(catalog.omitted_count)}</p>
      ) : null}
    </section>
  )
}
