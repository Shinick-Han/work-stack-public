import {
  OBSERVATION_HISTORY_UNAVAILABLE,
  OBSERVATION_HISTORY_UNSUPPORTED,
  classifyKnowledgeObservationFailure,
  describeKnowledgeObservationError,
  type ObservationBindingState,
  type SavedObservation,
} from '../../api/knowledgeObservation'
import type { VerificationEvidenceEntry } from '../../api/knowledgeVerification'

/**
 * Pure view-state transitions for the saved source-check history.
 *
 * The hook owns tokens, epochs, pending claims and the actual GET/POST. This module only
 * names what the surface believes after those events settle. It never reaches the network.
 */

/** How the owner answered the history read, which is what the surface can offer. */
export type SourceCheckHistoryMode =
  /** No capture identity to read for; the panel offers no source check at all. */
  | 'disabled'
  /** The read is still open. The source check stays disabled until it settles. */
  | 'loading'
  /** The owner published the saved-check routes. */
  | 'supported'
  /** Exactly a `not_found` 404: an owner from before these routes existed. */
  | 'legacy'
  /** Anything else. No old-server diagnosis is claimed, and no check is offered. */
  | 'unavailable'

/** The saved check as the view renders it. `entries` exist only for an unchanged binding. */
export interface SavedSourceCheck {
  checkedAt: string
  bindingState: ObservationBindingState
  entries: readonly VerificationEvidenceEntry[] | null
}

export interface HistoryViewState {
  key: string
  mode: SourceCheckHistoryMode
  loading: boolean
  pending: boolean
  saved: SavedSourceCheck | null
  error: string | null
  needsReload: boolean
}

/**
 * The wire observation as one displayable row set.
 *
 * A `changed` observation keeps its time and loses its evidence, which is the contract's
 * whole point: the capture no longer agrees with what was checked, so the old per-source
 * statuses must not be laid over the rows on screen.
 */
export function toSavedCheck(observation: SavedObservation | null): SavedSourceCheck | null {
  if (observation === null) return null
  return {
    checkedAt: observation.checked_at,
    bindingState: observation.binding_state,
    entries: observation.binding_state === 'unchanged' ? observation.result?.evidence ?? null : null,
  }
}

export function idleHistoryView(key: string, enabled: boolean): HistoryViewState {
  return {
    key,
    mode: enabled ? 'loading' : 'disabled',
    loading: false,
    pending: false,
    saved: null,
    error: null,
    needsReload: false,
  }
}

export function applyHistoryLoadStart(current: HistoryViewState): HistoryViewState {
  return { ...current, loading: true, error: null }
}

export function applyHistoryLoadSuccess(
  current: HistoryViewState,
  observation: SavedObservation | null,
): HistoryViewState {
  return {
    ...current,
    mode: 'supported',
    loading: false,
    saved: toSavedCheck(observation),
    error: null,
    needsReload: false,
  }
}

export function applyHistoryLoadFailure(current: HistoryViewState, failure: unknown): HistoryViewState {
  const legacy = classifyKnowledgeObservationFailure(failure).code === OBSERVATION_HISTORY_UNSUPPORTED
  return {
    ...current,
    mode: legacy ? 'legacy' : 'unavailable',
    loading: false,
    saved: null,
    // A legacy owner is a determination, not a failure: the old readonly check is
    // offered instead, so there is no error to raise. Every other outcome leaves
    // the surface undetermined and says so in one closed sentence.
    error: legacy ? null : describeKnowledgeObservationError(OBSERVATION_HISTORY_UNAVAILABLE),
    needsReload: false,
  }
}

/**
 * Clearing here, not on success, is what stops a failed check from leaving the
 * previous saved statuses on screen as if they were the answer to this click.
 */
export function applyHistoryRecordStart(current: HistoryViewState): HistoryViewState {
  return { ...current, pending: true, saved: null, error: null }
}

export function applyHistoryRecordSuccess(
  current: HistoryViewState,
  observation: SavedObservation,
): HistoryViewState {
  return {
    ...current,
    pending: false,
    saved: toSavedCheck(observation),
    error: null,
    needsReload: false,
  }
}

export function applyHistoryRecordFailure(current: HistoryViewState, failure: unknown): HistoryViewState {
  return {
    ...current,
    pending: false,
    saved: null,
    error: classifyKnowledgeObservationFailure(failure).message,
    // The owner may or may not have saved something. Reading is how that is
    // settled, and the reader asks for it — this hook never asks again by itself.
    needsReload: true,
  }
}
