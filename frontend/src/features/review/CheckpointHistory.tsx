import { useEffect, useState } from 'react'
import type {
  CheckpointAudit,
  CheckpointAuditEntry,
  CheckpointTransitionInput,
} from '../../domain/types'

/**
 * Daily Review checkpoint history and compensation controls.
 *
 * The whole workspace audit is validated by the caller before it arrives here;
 * this component only filters to the selected day for display. It never mutates
 * optimistically, never trims the explanation, and never resubmits by itself.
 */

const SUPERSEDE_CODES = ['incorrect', 'duplicate', 'obsolete'] as const

export interface FrozenAttempt {
  /** The Page lifetime (workspace and day) this attempt belongs to. */
  owner: string
  checkpointId: string
  /** The revision the confirmation was taken against; never silently rebased. */
  revision: number
  body: CheckpointTransitionInput
  idempotencyKey: string
}

/** Identity a staged confirmation is bound to; it never silently rebases. */
interface StagedIntent {
  rowId: string
  checkpointId: string
  revision: number
  state: string
  date: string
}

export interface CheckpointHistoryProps {
  audit: CheckpointAudit
  /** Display filter only, applied after the whole audit was validated. */
  date: string
  /** One attempt. Ambiguity is surfaced, never retried automatically. */
  onSubmit: (attempt: FrozenAttempt) => void
  /** Present only while an ambiguous attempt is awaiting an explicit retry. */
  pendingRetry?: FrozenAttempt | null
  onRetry?: (attempt: FrozenAttempt) => void
  onClearRetry?: () => void
  conflictMessage?: string | null
  /** Raw explanation of the last failed attempt, shown verbatim. */
  failedExplanation?: string | null
  /** Current Page owner; a staged intent never outlives it. */
  owner: string
  createIdempotencyKey: () => string
}

/** Opaque payloads are rendered defensively: they are not trusted content. */
function describeEntry(entry: unknown): string {
  if (entry === null || entry === undefined) return 'No entry content available'
  if (typeof entry === 'string') return entry
  try {
    const text = JSON.stringify(entry)
    return text === undefined ? 'Entry content is not displayable' : text
  } catch {
    return 'Entry content is not displayable'
  }
}

function entryKey(entry: CheckpointAuditEntry, index: number) {
  return entry.checkpoint_id ?? `legacy:${entry.locator.date}:${entry.locator.ordinal}:${index}`
}

/** A determinate refusal keeps its raw explanation readable, never normalized. */
function ConflictBanner(
  { explanation, message }: { explanation: string | null; message: string | null },
) {
  if (message === null) return null
  return (
    <>
      <p role="alert">{message}</p>
      {explanation === null ? null : (
        <label>
          <span>Submitted explanation</span>
          <input aria-label="Submitted explanation" readOnly value={explanation} />
        </label>
      )}
    </>
  )
}

/** Ambiguity shows the frozen snapshot and offers only its identical retry. */
function AmbiguityPanel(
  { attempt, onClear, onRetry }: {
    attempt: FrozenAttempt
    onClear?: () => void
    onRetry: (attempt: FrozenAttempt) => void
  },
) {
  return (
    <div role="status">
      <p>The transition may or may not have committed.</p>
      <label>
        <span>Frozen explanation</span>
        <input aria-label="Frozen explanation" readOnly value={attempt.body.reason.explanation} />
      </label>
      <button type="button" onClick={() => onRetry(attempt)}>
        Retry the same request
      </button>
      {onClear ? <button type="button" onClick={onClear}>Dismiss</button> : null}
    </div>
  )
}

/** Every recorded transition with its closed reason code and raw explanation. */
function TransitionList({ entry }: { entry: CheckpointAuditEntry }) {
  if (!entry.transitions.length) return null
  return (
    <ol aria-label={`Transitions for ${entry.checkpoint_id ?? 'legacy entry'}`}>
      {entry.transitions.map((transition) => (
        <li key={`${transition.checkpoint_id}:${transition.revision}`}>
          {`revision ${transition.revision} · ${transition.state} · `}
          {transition.reason.code}
          {` · ${transition.reason.explanation}`}
        </li>
      ))}
    </ol>
  )
}

export function CheckpointHistory({
  audit,
  date,
  onSubmit,
  pendingRetry = null,
  onRetry,
  onClearRetry,
  conflictMessage = null,
  failedExplanation = null,
  owner,
  createIdempotencyKey,
}: CheckpointHistoryProps) {
  // The staged intent is bound to the exact row it was opened against:
  // checkpoint, revision, state and day. Anything else is a different intent.
  const [staged, setStaged] = useState<StagedIntent | null>(null)
  const [code, setCode] = useState<string>(SUPERSEDE_CODES[0])
  const [explanation, setExplanation] = useState('')

  useEffect(() => {
    // Away-and-back must not resurrect an intent: it is cancelled, not hidden.
    setStaged(null)
  }, [owner])

  const dayEntries = audit.entries.filter((entry) => entry.locator.date === date)
  // A second action is refused while an ambiguous attempt is unresolved.
  const blocked = pendingRetry !== null

  return (
    <section className="checkpoint-history" aria-label="Checkpoint history">
      <h3>Checkpoint history</h3>
      <ConflictBanner explanation={failedExplanation} message={conflictMessage} />
      {pendingRetry && onRetry ? (
        <AmbiguityPanel attempt={pendingRetry} onClear={onClearRetry} onRetry={onRetry} />
      ) : null}

      {dayEntries.length === 0 ? (
        <p>No checkpoints recorded for this day.</p>
      ) : (
        <ul>
          {dayEntries.map((entry, index) => {
            const id = entryKey(entry, index)
            const superseded = entry.state === 'superseded'
            // Legacy rows have no checkpoint identity and no compensation.
            const canMutate = entry.checkpoint_id !== null
            return (
              <li key={id} data-checkpoint-state={entry.state}>
                <p>
                  <strong>{entry.checkpoint_id ?? 'Legacy entry'}</strong>
                  {` · ordinal ${entry.locator.ordinal} · ${entry.state}`}
                  {` · revision ${entry.revision}`}
                </p>
                <p>{describeEntry(entry.entry)}</p>

                <TransitionList entry={entry} />

                {canMutate ? (
                  // A refresh that changes revision, state or day drops the
                  // staged intent instead of resubmitting it against new state.
                  staged !== null
                  && staged.rowId === id
                  && staged.checkpointId === entry.checkpoint_id
                  && staged.revision === entry.revision
                  && staged.state === entry.state
                  && staged.date === entry.locator.date ? (
                    <form
                      aria-label={`Confirm ${superseded ? 'restore' : 'supersede'} ${entry.checkpoint_id}`}
                      onSubmit={(event) => {
                        event.preventDefault()
                        // Freeze CP, revision, raw body and key exactly once.
                        onSubmit({
                          checkpointId: entry.checkpoint_id as string,
                          revision: entry.revision,
                          body: {
                            state: superseded ? 'active' : 'superseded',
                            revision: entry.revision,
                            // Verbatim: server normalization is authoritative.
                            reason: {
                              code: superseded ? 'restore' : code,
                              explanation,
                            },
                          },
                          owner,
                          idempotencyKey: createIdempotencyKey(),
                        })
                        setStaged(null)
                      }}
                    >
                      {superseded ? null : (
                        <label>
                          <span>Reason</span>
                          <select
                            aria-label="Supersede reason code"
                            value={code}
                            onChange={(event) => setCode(event.target.value)}
                          >
                            {SUPERSEDE_CODES.map((value) => (
                              <option key={value} value={value}>{value}</option>
                            ))}
                          </select>
                        </label>
                      )}
                      <label>
                        <span>Explanation</span>
                        <input
                          aria-label="Explanation"
                          value={explanation}
                          onChange={(event) => setExplanation(event.target.value)}
                        />
                      </label>
                      <button type="submit">
                        {superseded ? 'Confirm restore' : 'Confirm supersede'}
                      </button>
                      <button type="button" onClick={() => setStaged(null)}>Cancel</button>
                    </form>
                  ) : (
                    <button
                      type="button"
                      disabled={blocked}
                      onClick={() => {
                        setStaged({
                          rowId: id,
                          checkpointId: entry.checkpoint_id as string,
                          revision: entry.revision,
                          state: entry.state,
                          date: entry.locator.date,
                        })
                        setExplanation('')
                      }}
                    >
                      {superseded ? `Restore ${entry.checkpoint_id}` : `Supersede ${entry.checkpoint_id}`}
                    </button>
                  )
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
