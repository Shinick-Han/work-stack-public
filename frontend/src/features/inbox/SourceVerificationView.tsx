import { Button } from '../../components/Primitives'
import { formatDateTime } from '../../utils/format'
import type { VerificationEvidenceEntry, VerificationStatus } from '../../api/knowledgeVerification'
import type { EvidenceObservation } from './useEvidenceVerification'
import type { SavedSourceCheck } from './useSourceCheckHistory'

/**
 * Presentation for one transient source check.
 *
 * Every string this file can render is written below. A status becomes one of eight
 * fixed phrases, a failure becomes one of the API module's closed sentences, and a
 * timestamp becomes the app's own `formatDateTime`. Nothing from the wire — no document
 * ref, no version string, no verifier code, no protocol name, no JSON — reaches the
 * screen, so a narrow drawer cannot be filled with an opaque identifier and a reader
 * cannot mistake a machine token for a finding.
 *
 * The phrasing is deliberately modest. `Matched when checked` is a statement about one
 * moment, not a durable claim; `File not found` says the verifier could not read the
 * file, never that the file was deleted; and no label anywhere here is a claim that the
 * captured summary is correct.
 */

export const SOURCE_CHECK_LABEL = 'Check source status'
export const SOURCE_CHECK_PENDING_LABEL = 'Checking source status…'
export const SOURCE_CHECK_NOTE =
  'A check reads the source once, at that moment. It does not update the saved capture and does not show that the summary is correct.'

const STATUS_LABELS: Readonly<Record<VerificationStatus, string>> = {
  current: 'Matched when checked',
  stale: 'Source changed',
  missing: 'File not found',
  unavailable: 'Source unavailable',
  denied: 'Access denied',
  refused: 'Source check refused',
  revoked: 'Access revoked',
  unverifiable: 'Could not verify',
}

export function sourceStatusLabel(status: VerificationStatus): string {
  return STATUS_LABELS[status]
}

/** The observation beside one evidence row. It sits next to the reported claims, never over them. */
export function SourceVerificationStatus({ entry }: { entry: VerificationEvidenceEntry }) {
  return (
    <p className="evidence-panel__observation">
      <span className={`evidence-status evidence-status--${entry.status}`}>{sourceStatusLabel(entry.status)}</span>
    </p>
  )
}

/**
 * The explicit transient action, its outcome line, and the standing caveat.
 *
 * `savedChecksUnavailable` is the one addition R27-E asks for here: on an owner from
 * before the saved-check routes existed this is still the only check available, and the
 * reader is told plainly that nothing will be kept. Callers that omit the flag — every
 * existing one — render exactly what they rendered before.
 */
export function SourceVerificationAction({
  error,
  observation,
  onCheck,
  pending,
  savedChecksUnavailable = false,
}: {
  error: string | null
  observation: EvidenceObservation | null
  onCheck: () => void
  pending: boolean
  savedChecksUnavailable?: boolean
}) {
  return (
    <div className="evidence-panel__check">
      <Button disabled={pending} icon="refresh" onClick={onCheck} variant="secondary">
        {pending ? SOURCE_CHECK_PENDING_LABEL : SOURCE_CHECK_LABEL}
      </Button>
      {savedChecksUnavailable ? (
        <p className="evidence-panel__checked">{SAVED_CHECKS_UNAVAILABLE_NOTE}</p>
      ) : null}
      {observation ? (
        <p className="evidence-panel__checked" role="status">
          Checked {formatDateTime(observation.checkedAt)}
        </p>
      ) : null}
      {error ? <p className="evidence-panel__check-error" role="alert">{error}</p> : null}
      <p className="evidence-panel__footnote">{SOURCE_CHECK_NOTE}</p>
    </div>
  )
}

/**
 * Presentation for the saved source check.
 *
 * Every sentence is written below, and every one of them is in the past tense on purpose.
 * `Last checked <time>` reports when an owner last looked; it is not a statement that the
 * sources are in that state now, and the standing note says so. A changed binding shows
 * the time and the one sentence explaining why the old per-source statuses are gone —
 * never the statuses themselves. `No saved source check.` reports what is retained right
 * now and claims nothing about what may or may not have been written.
 *
 * As on the transient surface, nothing from the wire reaches the screen: no document ref,
 * version string, verifier code, protocol name, format version or JSON.
 */
export const SAVED_SOURCE_CHECK_LABEL = 'Check and save source status'
export const RELOAD_SAVED_CHECK_LABEL = 'Reload saved check'
export const NO_SAVED_SOURCE_CHECK = 'No saved source check.'
export const SOURCE_BINDING_CHANGED_NOTE = 'Sources or access settings changed since this check.'
export const SAVED_CHECKS_UNAVAILABLE_NOTE = 'Saved checks are unavailable on this server.'
export const SAVED_SOURCE_CHECK_NOTE =
  'This is a saved record of a past source check. It does not show the sources as they are now, and it does not show that the summary is correct.'

export function SavedSourceCheckAction({
  error,
  loading,
  onRecord,
  onReload,
  pending,
  reloadable,
  saved,
  supported,
}: {
  error: string | null
  loading: boolean
  onRecord: () => void
  onReload: () => void
  pending: boolean
  /** An explicit reload has to run before another check can be recorded. */
  reloadable: boolean
  saved: SavedSourceCheck | null
  /** The owner published the saved-check routes and the read has settled. */
  supported: boolean
}) {
  // While the history is still open, or undetermined, or waiting on an explicit reload,
  // recording is not offered: a new check must never be the way a reader discovers what
  // the server already kept.
  const canRecord = supported && !loading && !pending && !reloadable
  // An unconfirmed save is not an observed absence. Wait for the explicit history read.
  const settled = canRecord
  return (
    <div className="evidence-panel__check">
      <Button disabled={!canRecord} icon="refresh" onClick={onRecord} variant="secondary">
        {pending ? SOURCE_CHECK_PENDING_LABEL : SAVED_SOURCE_CHECK_LABEL}
      </Button>
      {settled ? (
        <p className="evidence-panel__checked" role="status">
          {saved ? `Last checked ${formatDateTime(saved.checkedAt)}` : NO_SAVED_SOURCE_CHECK}
        </p>
      ) : null}
      {settled && saved?.bindingState === 'changed' ? (
        <p className="evidence-panel__checked">{SOURCE_BINDING_CHANGED_NOTE}</p>
      ) : null}
      {error ? <p className="evidence-panel__check-error" role="alert">{error}</p> : null}
      {reloadable ? (
        <Button disabled={loading || pending} icon="refresh" onClick={onReload} variant="secondary">
          {RELOAD_SAVED_CHECK_LABEL}
        </Button>
      ) : null}
      <p className="evidence-panel__footnote">{SAVED_SOURCE_CHECK_NOTE}</p>
    </div>
  )
}
