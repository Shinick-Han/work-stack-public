import { Pill } from '../../components/Primitives'
import type { CheckpointAuditEntry } from '../../domain/types'
import {
  CHECKPOINT_FACT_FIELDS,
  CHECKPOINT_FACT_LABELS,
  checkpointStateLabel,
  summarizeCheckpointEntry,
  type CheckpointEntrySummary,
  type CheckpointExtra,
  type CheckpointFactField,
} from './checkpointEntrySummary'

/**
 * The human-facing face of one recorded checkpoint.
 *
 * What the reader came for — the Task, its status, and what was done, planned
 * and blocked — is the headline. The checkpoint id, ordinal and revision are
 * kept on the card because an audit is worthless without them, but they read as
 * provenance rather than as the content.
 *
 * Recognition is partial, so anything the summary could not render is offered
 * below it as collapsed "Additional recorded data" — present and inspectable,
 * but never the body of the card and never raw markup.
 */

/** Provenance: always available, never the headline. */
function EntryIdentifiers({ entry }: { entry: CheckpointAuditEntry }) {
  return (
    <p className="checkpoint-entry__identifiers">
      <strong>{entry.checkpoint_id ?? 'Legacy entry'}</strong>
      <span>{`ordinal ${entry.locator.ordinal}`}</span>
      <span>{`revision ${entry.revision}`}</span>
    </p>
  )
}

/** An empty list says so; it is never confused with an unreported one. */
function FactGroup({ field, items }: { field: CheckpointFactField; items: readonly string[] }) {
  return (
    <div className={`checkpoint-facts checkpoint-facts--${field}`}>
      <dt>{CHECKPOINT_FACT_LABELS[field]}</dt>
      <dd>
        {items.length === 0 ? (
          <span className="checkpoint-facts__none">None recorded</span>
        ) : (
          <ul>
            {items.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}
          </ul>
        )}
      </dd>
    </div>
  )
}

/**
 * Unrecognized fields and unrenderable list items, kept discoverable.
 *
 * Collapsed by default so it never competes with the recorded work, and every
 * label and value is a JSX text child, so a payload carrying markup is shown as
 * the characters it contains rather than parsed as HTML.
 */
function ExtraData({ extras }: { extras: readonly CheckpointExtra[] }) {
  if (extras.length === 0) return null
  return (
    <details className="checkpoint-entry__extras">
      <summary>{`Additional recorded data (${extras.length})`}</summary>
      <dl>
        {extras.map((extra, index) => (
          <div key={`${index}:${extra.label}`}>
            <dt>{extra.label}</dt>
            <dd>{extra.value}</dd>
          </div>
        ))}
      </dl>
    </details>
  )
}

function EntryBody({ summary }: { summary: CheckpointEntrySummary }) {
  return (
    <>
      {summary.readable ? (
        <dl className="checkpoint-entry__facts">
          {CHECKPOINT_FACT_FIELDS.map((field) => (
            <FactGroup field={field} items={summary[field]} key={field} />
          ))}
        </dl>
      ) : (
        <p className="checkpoint-entry__fallback">{summary.fallback}</p>
      )}
      <ExtraData extras={summary.extras} />
    </>
  )
}

export function CheckpointEntryCard({ entry }: { entry: CheckpointAuditEntry }) {
  const summary = summarizeCheckpointEntry(entry.entry)
  const status = checkpointStateLabel(entry.state)
  return (
    <article className="checkpoint-entry">
      <header className="checkpoint-entry__header">
        <span className="checkpoint-entry__task">
          <strong>{summary.taskTitle ?? 'Untitled checkpoint entry'}</strong>
          {summary.taskId === null ? null : <small>{summary.taskId}</small>}
        </span>
        <span className="checkpoint-entry__status">
          <span className="sr-only">{'Status '}</span>
          <Pill tone={entry.state === 'superseded' ? 'dropped' : 'open'}>{status}</Pill>
        </span>
      </header>
      <EntryBody summary={summary} />
      <EntryIdentifiers entry={entry} />
    </article>
  )
}
