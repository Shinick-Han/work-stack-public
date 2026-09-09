import type { KnowledgeConnectionPolicy, KnowledgeOccupancy } from '../../api/knowledge'

/**
 * What the launcher knows about the stored owner policy, and the two questions the rest
 * of the screen asks about it.
 *
 * This is a state model, not a state machine: it holds no state, performs no I/O, reads
 * no clock and knows nothing about the launcher that owns the transitions. Only a
 * successful read establishes what the policy holds, so both predicates here are about
 * the difference between "read" and "still believed".
 */

type PolicyStatus = 'idle' | 'loading' | 'ready' | 'error'

export interface PolicyState {
  error: string | null
  policy: KnowledgeConnectionPolicy | null
  /**
   * `policy` is the last read that succeeded, and a later required read did not answer.
   * What the server holds now is unknown, so nothing here may be written or asked against
   * it until a read succeeds again.
   */
  stale: boolean
  status: PolicyStatus
  /**
   * What the request store held when the policy on screen was read or saved, if the
   * server published a valid observation with it. It is kept across a read that did not
   * answer — marked out of date rather than discarded — and dropped entirely by a reset,
   * because a snapshot of another workspace's store is not this workspace's.
   */
  usage: KnowledgeOccupancy | null
}

export const IDLE_POLICY: PolicyState = {
  error: null,
  policy: null,
  stale: false,
  status: 'idle',
  usage: null,
}

/** What the settings form and the chooser may act on: a policy an actual read returned. */
export function isAuthoritative(state: PolicyState): boolean {
  return state.status === 'ready' && state.policy !== null && !state.stale
}

/** The observation is not the current capacity while a read is open or has failed. */
export function usageIsOutdated(state: PolicyState): boolean {
  return state.stale || state.status === 'loading'
}
