/**
 * The recorded-progress facts the resume brief consumes.
 *
 * UI-B owns deriving these from the checkpoint audit and UI-C adapts its `TaskResumeFacts`
 * onto this shape; UI-D only consumes it. The knowledge feature must not import the tasks
 * feature, so this stays a local consumer contract rather than a re-export.
 *
 * Done/Next/Blockers stay arrays so multi-line and list-shaped records survive the trip
 * losslessly. The contract is deliberately explicit about not-yet-known, unreadable,
 * absent and partially readable records, so a brief never presents an inferred all-clear.
 *
 * `digest` carries UI-B's `facts.version` verbatim: it changes if and only if the selected
 * record's identity or content changes. A checkpoint can change without the Task revision
 * changing, so preparation ownership keys on this digest as well as on the Task binding.
 */

export interface ResumeProgressSnapshot {
  /** Null on a legacy record that predates checkpoint identity; no ID is invented for it. */
  checkpointId: string | null
  /** Recorded calendar day exactly as stored. No time of day is invented. */
  recordedDate: string
  ordinal: number | null
  revision: number | null
  digest: string
  done: readonly string[]
  next: readonly string[]
  blockers: readonly string[]
}

export type ResumeProgressFacts =
  | { status: 'loading' }
  | { status: 'unavailable'; reason: string }
  | { status: 'none' }
  | { status: 'partial'; snapshot: ResumeProgressSnapshot; missing: readonly string[] }
  | { status: 'ready'; snapshot: ResumeProgressSnapshot }

export const UNBOUND_RESUME_PROGRESS: ResumeProgressFacts = {
  status: 'unavailable',
  reason: 'Recorded progress was not available in this view.',
}

export const NO_PROGRESS_COPY = 'No progress recorded yet.'
export const NO_NEXT_COPY = 'No next step recorded in this checkpoint.'
export const NO_BLOCKERS_COPY = 'No blockers recorded in this checkpoint.'
export const OMIT_PROGRESS_COPY = 'Prepare this brief without a recorded progress snapshot.'
export const INCLUDE_PARTIAL_PROGRESS_COPY =
  'Include the partially readable checkpoint and state what could not be read.'

export function resumeProgressSnapshot(facts: ResumeProgressFacts) {
  return facts.status === 'ready' || facts.status === 'partial' ? facts.snapshot : null
}

/**
 * Identity of the progress content a brief was prepared against. Comparing keys is how a
 * checkpoint-only edit marks an already prepared brief stale.
 */
export function resumeProgressKey(facts: ResumeProgressFacts) {
  if (facts.status === 'ready') return `record:${facts.snapshot.digest}`
  if (facts.status === 'partial') return `partial:${facts.snapshot.digest}:${[...facts.missing].sort().join(',')}`
  if (facts.status === 'none') return 'none'
  if (facts.status === 'unavailable') return `unavailable:${facts.reason}`
  return 'loading'
}

/**
 * A brief prepared before the facts settled is not stale merely because they are being
 * re-read; it is stale once a different settled value is known.
 */
export function resumeProgressChanged(preparedKey: string, current: ResumeProgressFacts) {
  const currentKey = resumeProgressKey(current)
  if (currentKey === 'loading') return false
  return currentKey !== preparedKey
}

/**
 * Leaving the recorded progress out, or carrying a record that could not be read in full,
 * is the user's explicit choice. It is never a silent default of pressing Prepare.
 */
export function progressOmissionAcknowledgement(facts: ResumeProgressFacts) {
  if (facts.status === 'unavailable') return OMIT_PROGRESS_COPY
  if (facts.status === 'partial') return INCLUDE_PARTIAL_PROGRESS_COPY
  return null
}

export function checkpointAttribution(snapshot: ResumeProgressSnapshot) {
  return `From the latest checkpoint · ${snapshot.recordedDate}`
}

/** Names the record without inventing an identifier a legacy row never had. */
export function checkpointProvenanceLabel(snapshot: ResumeProgressSnapshot) {
  if (snapshot.checkpointId) return `Checkpoint ${snapshot.checkpointId}`
  const slot = snapshot.ordinal === null ? '' : ` #${snapshot.ordinal}`
  return `Legacy record · ${snapshot.recordedDate}${slot}`
}
