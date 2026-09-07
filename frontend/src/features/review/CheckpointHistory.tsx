import { useEffect, useState } from 'react'
import type {
  CheckpointAudit,
  CheckpointAuditEntry,
  CheckpointTransitionEventRecord,
  CheckpointTransitionInput,
} from '../../domain/types'
import { CheckpointEntryCard } from './CheckpointEntryCard'

/**
 * Daily Review checkpoint history and compensation controls.
 *
 * The whole workspace audit is validated by the caller before it arrives here;
 * this component only filters to the selected day for display. It never mutates
 * optimistically, never trims the explanation, and never resubmits by itself.
 *
 * The payload itself is read by CheckpointEntryCard, which is the only place
 * that interprets it. This file stays responsible for identity, staging and the
 * one frozen attempt.
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

/** The one shared draft: a staged row is unique, so its inputs can be too. */
interface SupersedeDraft {
  code: string
  setCode: (value: string) => void
  explanation: string
  setExplanation: (value: string) => void
}

interface CompensationProps {
  entry: CheckpointAuditEntry
  rowId: string
  blocked: boolean
  staged: StagedIntent | null
  onStage: (intent: StagedIntent | null) => void
  draft: SupersedeDraft
  onSubmit: (attempt: FrozenAttempt) => void
  owner: string
  createIdempotencyKey: () => string
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

function entryKey(entry: CheckpointAuditEntry, index: number) {
  return entry.checkpoint_id ?? `legacy:${entry.locator.date}:${entry.locator.ordinal}:${index}`
}

/**
 * A refresh that changes revision, state or day is a DIFFERENT intent: the
 * staged confirmation is dropped rather than resubmitted against new state.
 */
function isStagedOn(staged: StagedIntent | null, entry: CheckpointAuditEntry, rowId: string) {
  return staged !== null
    && staged.rowId === rowId
    && staged.checkpointId === entry.checkpoint_id
    && staged.revision === entry.revision
    && staged.state === entry.state
    && staged.date === entry.locator.date
}

/** A determinate refusal keeps its raw explanation readable, never normalized. */
function ConflictBanner(
  { explanation, message }: { explanation: string | null; message: string | null },
) {
  if (message === null) return null
  return (
    <>
      <p className="checkpoint-history__alert" role="alert">{message}</p>
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
    <div className="checkpoint-history__ambiguity" role="status">
      <p>The transition may or may not have committed.</p>
      <label>
        <span>Frozen explanation</span>
        <input aria-label="Frozen explanation" readOnly value={attempt.body.reason.explanation} />
      </label>
      <button className="button button--secondary" type="button" onClick={() => onRetry(attempt)}>
        Retry the same request
      </button>
      {onClear ? (
        <button className="button button--ghost" type="button" onClick={onClear}>Dismiss</button>
      ) : null}
    </div>
  )
}

/** What each transition did, in words, with its code and raw explanation. */
function transitionSentence(transition: CheckpointTransitionEventRecord) {
  const action = transition.type === 'worklog.restored' ? 'Restored' : 'Superseded'
  return `${action} at revision ${transition.revision} · ${transition.reason.code}`
    + ` · ${transition.reason.explanation}`
}

/** Every recorded transition with its closed reason code and raw explanation. */
function TransitionList({ entry }: { entry: CheckpointAuditEntry }) {
  if (!entry.transitions.length) return null
  return (
    <ol
      aria-label={`Transitions for ${entry.checkpoint_id ?? 'legacy entry'}`}
      className="checkpoint-history__transitions"
    >
      {entry.transitions.map((transition) => (
        <li key={`${transition.checkpoint_id}:${transition.revision}`}>
          {transitionSentence(transition)}
        </li>
      ))}
    </ol>
  )
}

function ConfirmForm(
  { entry, draft, onStage, onSubmit, owner, createIdempotencyKey }: CompensationProps,
) {
  const superseded = entry.state === 'superseded'
  return (
    <form
      aria-label={`Confirm ${superseded ? 'restore' : 'supersede'} ${entry.checkpoint_id}`}
      className="checkpoint-entry__confirm"
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
              code: superseded ? 'restore' : draft.code,
              explanation: draft.explanation,
            },
          },
          owner,
          idempotencyKey: createIdempotencyKey(),
        })
        onStage(null)
      }}
    >
      {superseded ? null : (
        <label>
          <span>Reason</span>
          <select
            aria-label="Supersede reason code"
            value={draft.code}
            onChange={(event) => draft.setCode(event.target.value)}
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
          value={draft.explanation}
          onChange={(event) => draft.setExplanation(event.target.value)}
        />
      </label>
      <button className="button button--primary" type="submit">
        {superseded ? 'Confirm restore' : 'Confirm supersede'}
      </button>
      <button className="button button--ghost" type="button" onClick={() => onStage(null)}>
        Cancel
      </button>
    </form>
  )
}

/** Compensation stays reachable on every real row, but never leads the card. */
function CompensationControl(props: CompensationProps) {
  const { entry, rowId, blocked, staged, onStage, draft } = props
  // Legacy rows have no checkpoint identity and no compensation.
  if (entry.checkpoint_id === null) return null
  if (isStagedOn(staged, entry, rowId)) return <ConfirmForm {...props} />
  const superseded = entry.state === 'superseded'
  return (
    <button
      className="button button--ghost checkpoint-entry__action"
      type="button"
      disabled={blocked}
      onClick={() => {
        onStage({
          rowId,
          checkpointId: entry.checkpoint_id as string,
          revision: entry.revision,
          state: entry.state,
          date: entry.locator.date,
        })
        draft.setExplanation('')
      }}
    >
      {superseded ? `Restore ${entry.checkpoint_id}` : `Supersede ${entry.checkpoint_id}`}
    </button>
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
  const draft: SupersedeDraft = { code, setCode, explanation, setExplanation }

  return (
    <section className="checkpoint-history" aria-label="Checkpoint history">
      <h3>Checkpoint history</h3>
      <ConflictBanner explanation={failedExplanation} message={conflictMessage} />
      {pendingRetry && onRetry ? (
        <AmbiguityPanel attempt={pendingRetry} onClear={onClearRetry} onRetry={onRetry} />
      ) : null}

      {dayEntries.length === 0 ? (
        <p className="checkpoint-history__empty">No checkpoints recorded for this day.</p>
      ) : (
        <ul className="checkpoint-history__list">
          {dayEntries.map((entry, index) => {
            const rowId = entryKey(entry, index)
            return (
              <li key={rowId} data-checkpoint-state={entry.state}>
                <CheckpointEntryCard entry={entry} />
                <TransitionList entry={entry} />
                <CompensationControl
                  blocked={blocked}
                  createIdempotencyKey={createIdempotencyKey}
                  draft={draft}
                  entry={entry}
                  onStage={setStaged}
                  onSubmit={onSubmit}
                  owner={owner}
                  rowId={rowId}
                  staged={staged}
                />
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
