import { Button } from '../../components/Primitives'
import type { KnowledgeConnection } from '../../api/knowledge'
import {
  EMPTY_CONNECTION_ROW,
  MAX_POLICY_CONNECTIONS,
  type ConnectionPolicyRow,
} from './knowledgeConnectionPolicy'

/**
 * The owner's nonsecret connection policy, as a form.
 *
 * Three fields per connection and no fourth: an **alias**, the **upstream workspace ID**
 * the alias stands for, and the **corpus aliases** it grants. There is deliberately no
 * endpoint, host, share, drive, local source path, token, password or provider field
 * anywhere on this screen, no provider-specific default, and no control that would add
 * one. `scope` is not offered either — the server supplies the single legal scope, and a
 * body cannot spell a project-level grant.
 *
 * This component is presentation only. It holds the typed rows its parent gives it, and
 * the parent owns the compare-and-set write, the refetch and every decision about what a
 * refusal means. Saving is always an explicit press, and the consequence is stated on
 * screen before it: replacing the roster advances the policy revision, which is exactly
 * how outstanding requests stop being authorised.
 */

const SAVE_CONSEQUENCE =
  'Saving replaces the whole connection roster and advances the policy revision. Requests already issued under the previous revision stop being authorised, and any receipt still on screen is cleared.'
const NONSECRET_NOTE =
  'These are labels and workspace identifiers only. Work Stack never asks for an endpoint, a folder, a drive, an account or a token here, and it contacts no provider.'
/**
 * Shown for every `held` review, so it may claim only what a successful read establishes:
 * the roster the server returned. Who wrote last, and whether the previous save landed,
 * are not read from a roster — the outcome line the parent installs owns that, and this
 * points at it rather than restating it.
 */
const HELD_REVIEW_NOTE =
  'Review the policy returned by the server before saving again. The outcome of the previous save is shown above.'
const UNREAD_NOTE =
  'The policy could not be re-read afterwards, so what the server holds now is not known. It is not known to be empty either — nothing was read at all. Nothing here was written or sent again.'
const UNKNOWN_REVISION_NOTE =
  'The policy revision the server holds is not known, so there is nothing to save against yet.'
const REREAD_LABEL = 'Read the policy again'

/**
 * What a follow-up read established. `unknown` is a first-class answer: a read that did
 * not answer is never flattened into an empty roster, because only a successful read can
 * establish that the server holds nothing.
 */
export type KnowledgePolicyReview =
  | { kind: 'held'; connections: readonly KnowledgeConnection[] }
  | { kind: 'unknown' }

export interface KnowledgeConnectionSettingsProps {
  rows: ConnectionPolicyRow[]
  onRowsChange: (rows: ConnectionPolicyRow[]) => void
  /** The revision a read actually returned, or `null` when the current one is unknown. */
  policyRevision: number | null
  saving: boolean
  error: string | null
  notice: string | null
  /** What the server holds, after a lost compare-and-set or an unconfirmed save. */
  review: KnowledgePolicyReview | null
  onSave: () => void
  onCancel: () => void
  /** Asks for the authoritative read that a save is held on. */
  onReread: () => void
}

function ConnectionRow({
  index,
  onChange,
  onRemove,
  row,
}: {
  index: number
  onChange: (next: ConnectionPolicyRow) => void
  onRemove: () => void
  row: ConnectionPolicyRow
}) {
  return (
    <fieldset className="knowledge-settings__row">
      <legend>Connection {index + 1}</legend>
      <label className="field">
        <span>Alias</span>
        <input
          autoComplete="off"
          onChange={(event) => onChange({ ...row, alias: event.target.value })}
          placeholder="team-nas"
          spellCheck={false}
          value={row.alias}
        />
      </label>
      <label className="field">
        <span>Upstream workspace ID</span>
        <input
          autoComplete="off"
          onChange={(event) => onChange({ ...row, upstreamWorkspaceUid: event.target.value })}
          placeholder="66666666-6666-4666-8666-666666666666"
          spellCheck={false}
          value={row.upstreamWorkspaceUid}
        />
      </label>
      <label className="field">
        <span>Corpus aliases</span>
        <input
          autoComplete="off"
          onChange={(event) => onChange({ ...row, corpusRefs: event.target.value })}
          placeholder="nas-team-share, product-notes"
          spellCheck={false}
          value={row.corpusRefs}
        />
      </label>
      <div className="knowledge-settings__row-actions">
        <Button onClick={onRemove} variant="ghost">Remove connection {index + 1}</Button>
      </div>
    </fieldset>
  )
}

function ServerPolicyReview({ review }: { review: KnowledgePolicyReview }) {
  if (review.kind === 'unknown') {
    // The one thing this block must never do is render an unread policy as an empty one.
    // The read that would settle it is the explicit control in the actions below.
    return (
      <section aria-label="Policy the server holds" className="knowledge-settings__review">
        <h4>What the server holds now is unknown</h4>
        <p>{UNREAD_NOTE}</p>
      </section>
    )
  }
  return (
    <section aria-label="Policy the server holds" className="knowledge-settings__review">
      <h4>What the server holds now</h4>
      {review.connections.length ? (
        <ul>
          {review.connections.map((connection) => (
            <li key={connection.alias}>
              <strong>{connection.alias}</strong>
              <small>{connection.corpus_refs.join(' · ')}</small>
            </li>
          ))}
        </ul>
      ) : (
        <p>No connection at all.</p>
      )}
    </section>
  )
}

export function KnowledgeConnectionSettings({
  error,
  notice,
  onCancel,
  onReread,
  onRowsChange,
  onSave,
  policyRevision,
  review,
  rows,
  saving,
}: KnowledgeConnectionSettingsProps) {
  const replaceRow = (index: number, next: ConnectionPolicyRow) =>
    onRowsChange(rows.map((row, position) => (position === index ? next : row)))
  // A compare-and-set needs a revision a read returned. Without one there is nothing
  // honest to write against, so saving waits for the read rather than assuming zero.
  const known = policyRevision !== null
  return (
    <section aria-label="Knowledge connection settings" className="knowledge-settings">
      <p className="knowledge-settings__lede">{NONSECRET_NOTE}</p>
      <p className="field-help">
        {known ? `Policy revision ${policyRevision}.` : UNKNOWN_REVISION_NOTE}
      </p>
      {rows.map((row, index) => (
        <ConnectionRow
          index={index}
          key={index}
          onChange={(next) => replaceRow(index, next)}
          onRemove={() => onRowsChange(rows.filter((_row, position) => position !== index))}
          row={row}
        />
      ))}
      <div className="knowledge-settings__add">
        <Button
          disabled={rows.length >= MAX_POLICY_CONNECTIONS}
          icon="plus"
          onClick={() => onRowsChange([...rows, { ...EMPTY_CONNECTION_ROW }])}
          variant="ghost"
        >
          Add connection
        </Button>
      </div>
      {review ? <ServerPolicyReview review={review} /> : null}
      {error ? <p className="inline-error" role="alert">{error}</p> : null}
      {notice ? <p className="knowledge-settings__notice" role="status">{notice}</p> : null}
      {review?.kind === 'held' ? (
        // Below the outcome line on purpose: the sentence points at what the parent said
        // about the previous save, and must not be read before it.
        <p className="knowledge-settings__notice" role="status">{HELD_REVIEW_NOTE}</p>
      ) : null}
      <p className="knowledge-settings__consequence">{SAVE_CONSEQUENCE}</p>
      <div className="knowledge-settings__actions">
        <Button onClick={onCancel} variant="ghost">Cancel</Button>
        {known ? null : (
          <Button icon="refresh" onClick={onReread} variant="secondary">{REREAD_LABEL}</Button>
        )}
        <Button disabled={saving || !known} icon="check" onClick={onSave} variant="primary">
          {saving ? 'Saving…' : 'Save connection policy'}
        </Button>
      </div>
    </section>
  )
}
