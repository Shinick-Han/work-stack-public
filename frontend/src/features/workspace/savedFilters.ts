import { z } from 'zod'
import type { AppUrlState } from '../../domain/types'
import {
  normalizeDoneVisibility,
  type DoneVisibility,
} from './views/completedTaskVisibility'
import {
  normalizeOutcomeFilter,
  type OutcomeFilter,
} from './views/keyResultModel'

export const SAVED_FILTERS_KEY = 'workstack:saved-filters:v1'
const MAX_SAVED_FILTERS = 12

const savedFilterSchema = z.object({
  id: z.string().min(1).max(80),
  name: z.string().min(1).max(120),
  objectiveId: z.string().max(80),
  priority: z.enum(['all', 'P0', 'P1', 'P2', 'P3']),
  readiness: z.enum(['all', 'ready', 'blocked']).default('all'),
  timing: z.enum(['all', 'overdue', 'today', 'soon', 'unscheduled']).default('all'),
  search: z.string().max(200),
  status: z.enum(['all', 'open', 'started', 'done', 'dropped']),
  view: z.enum(['graph', 'board', 'treemap', 'table']),
  // The key is declared so `.strict()` still rejects unrelated unknown keys,
  // but its value is accepted permissively: an invalid stored coordinate must
  // normalize to 'default' rather than discard an otherwise valid row.
  doneVisibility: z.unknown().optional(),
  // Declared so `.strict()` still rejects unrelated unknown keys, and accepted
  // permissively: an invalid stored coordinate normalizes to all rather than
  // discarding an otherwise valid row.
  outcomeFilter: z.unknown().optional(),
}).strict()

const savedFiltersSchema = z.array(savedFilterSchema).max(MAX_SAVED_FILTERS)
type StoredSavedFilter = z.infer<typeof savedFilterSchema>

/**
 * A saved filter. The coordinate is optional on input so existing row
 * constructions stay valid; every value this module returns is normalized.
 */
export type SavedFilter = Omit<StoredSavedFilter, 'doneVisibility' | 'outcomeFilter'> & {
  doneVisibility?: DoneVisibility
  outcomeFilter?: OutcomeFilter
}

/** A saved filter that has been normalized, so its coordinate is concrete. */
export type NormalizedSavedFilter = SavedFilter & {
  doneVisibility: DoneVisibility
  outcomeFilter: OutcomeFilter
}

/**
 * A stored row that predates the coordinate keeps its meaning: a saved All view
 * becomes a durable show, anything else becomes default. This is the SAVED-ROW
 * rule, deliberately different from a current in-memory state where a status of
 * 'all' alone never implies show. An invalid supplied value is not absence, so
 * the shared normalizer resolves it to 'default' without touching the row.
 */
function normalizeSavedRow(row: StoredSavedFilter): NormalizedSavedFilter {
  return {
    ...row,
    doneVisibility: normalizeDoneVisibility(row.doneVisibility, row.status === 'all'),
    outcomeFilter: normalizeOutcomeFilter(row.outcomeFilter),
  }
}

export function readSavedFilters(): NormalizedSavedFilter[] {
  if (typeof window === 'undefined') return []
  try {
    const value = window.localStorage.getItem(SAVED_FILTERS_KEY)
    // Normalization is read-only: storage is never written back here.
    return value ? savedFiltersSchema.parse(JSON.parse(value)).map(normalizeSavedRow) : []
  } catch {
    window.localStorage.removeItem(SAVED_FILTERS_KEY)
    return []
  }
}

export function writeSavedFilters(filters: SavedFilter[]): NormalizedSavedFilter[] {
  // An explicit save always persists a concrete coordinate, including 'default',
  // so a later read can never confuse a new default with a legacy omission.
  const bounded = savedFiltersSchema
    .parse(filters.slice(-MAX_SAVED_FILTERS))
    .map(normalizeSavedRow)
  if (typeof window !== 'undefined') {
    if (bounded.length) window.localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify(bounded))
    else window.localStorage.removeItem(SAVED_FILTERS_KEY)
  }
  return bounded
}

export function filterCoordinates(state: AppUrlState) {
  return {
    // Current state, not a stored row: an omitted coordinate is 'default' even
    // when status is 'all'. Every explicit save therefore stores a concrete enum.
    doneVisibility: normalizeDoneVisibility(state.doneVisibility),
    objectiveId: state.objectiveId,
    outcomeFilter: normalizeOutcomeFilter(state.outcomeFilter),
    priority: state.priority,
    readiness: state.readiness,
    timing: state.timing,
    search: state.search.slice(0, 200),
    status: state.status,
    view: state.view,
  }
}

function sameOutcomeFilter(left: OutcomeFilter, right: OutcomeFilter): boolean {
  if (left.kind !== right.kind) return false
  if (left.kind !== 'pair' || right.kind !== 'pair') return true
  return left.objectiveId === right.objectiveId && left.keyResultId === right.keyResultId
}

export function sameSavedFilter(filter: SavedFilter, state: AppUrlState): boolean {
  const coordinates = filterCoordinates(state)
  return normalizeDoneVisibility(filter.doneVisibility, filter.status === 'all')
      === coordinates.doneVisibility
    && filter.objectiveId === coordinates.objectiveId
    && sameOutcomeFilter(
      normalizeOutcomeFilter(filter.outcomeFilter),
      coordinates.outcomeFilter,
    )
    && filter.priority === coordinates.priority
    && filter.readiness === coordinates.readiness
    && filter.timing === coordinates.timing
    && filter.search === coordinates.search
    && filter.status === coordinates.status
    && filter.view === coordinates.view
}
