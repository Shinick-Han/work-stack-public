import { z } from 'zod'
import type { AppUrlState } from '../../domain/types'

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
}).strict()

const savedFiltersSchema = z.array(savedFilterSchema).max(MAX_SAVED_FILTERS)
export type SavedFilter = z.infer<typeof savedFilterSchema>

export function readSavedFilters(): SavedFilter[] {
  if (typeof window === 'undefined') return []
  try {
    const value = window.localStorage.getItem(SAVED_FILTERS_KEY)
    return value ? savedFiltersSchema.parse(JSON.parse(value)) : []
  } catch {
    window.localStorage.removeItem(SAVED_FILTERS_KEY)
    return []
  }
}

export function writeSavedFilters(filters: SavedFilter[]): SavedFilter[] {
  const bounded = savedFiltersSchema.parse(filters.slice(-MAX_SAVED_FILTERS))
  if (typeof window !== 'undefined') {
    if (bounded.length) window.localStorage.setItem(SAVED_FILTERS_KEY, JSON.stringify(bounded))
    else window.localStorage.removeItem(SAVED_FILTERS_KEY)
  }
  return bounded
}

export function filterCoordinates(state: AppUrlState) {
  return {
    objectiveId: state.objectiveId,
    priority: state.priority,
    readiness: state.readiness,
    timing: state.timing,
    search: state.search.slice(0, 200),
    status: state.status,
    view: state.view,
  }
}

export function sameSavedFilter(filter: SavedFilter, state: AppUrlState): boolean {
  const coordinates = filterCoordinates(state)
  return filter.objectiveId === coordinates.objectiveId
    && filter.priority === coordinates.priority
    && filter.readiness === coordinates.readiness
    && filter.timing === coordinates.timing
    && filter.search === coordinates.search
    && filter.status === coordinates.status
    && filter.view === coordinates.view
}
