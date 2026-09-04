import { useCallback, useEffect, useRef, useState } from 'react'
import {
  TASK_PRIORITIES,
  TASK_STATUSES,
  WORKSPACE_VIEWS,
  type AppUrlState,
  type NormalizedAppUrlState,
} from '../domain/types'
import { normalizeDoneVisibility } from '../features/workspace/views/completedTaskVisibility'
import {
  decodeOutcomeFilter,
  encodeOutcomeFilter,
  normalizeOutcomeFilter,
} from '../features/workspace/views/keyResultModel'

const defaultState: NormalizedAppUrlState = {
  surface: 'workspace',
  view: 'graph',
  search: '',
  status: 'all',
  priority: 'all',
  readiness: 'all',
  timing: 'all',
  objectiveId: 'all',
  outcomeFilter: { kind: 'all' },
  taskId: null,
  captureId: null,
  doneVisibility: 'default',
}

const surfaces = ['workspace', 'focus', 'inbox', 'review', 'objectives'] as const

function oneOf<T extends string>(value: string | null, values: readonly T[], fallback: T): T {
  return value && values.includes(value as T) ? (value as T) : fallback
}

export function readUrlState(search = window.location.search): NormalizedAppUrlState {
  const params = new URLSearchParams(search)
  // Legacy inference reads the LITERAL query parameter, never the normalized
  // status value, so a fresh or programmatic state with status 'all' stays
  // 'default' while an old ?status=all deep link becomes a durable 'show'.
  const legacyExplicitAll = params.get('status') === 'all'
  const suppliedVisibility = params.has('doneVisibility')
    ? params.get('doneVisibility')
    : undefined
  return normalizeUrlState({
    surface: oneOf(params.get('surface'), surfaces, defaultState.surface),
    view: oneOf(params.get('view'), WORKSPACE_VIEWS, defaultState.view),
    search: params.get('q') ?? '',
    status: oneOf(params.get('status'), ['all', ...TASK_STATUSES], 'all'),
    priority: oneOf(params.get('priority'), ['all', ...TASK_PRIORITIES], 'all'),
    readiness: oneOf(params.get('readiness'), ['all', 'ready', 'blocked'], 'all'),
    timing: oneOf(params.get('timing'), ['all', 'overdue', 'today', 'soon', 'unscheduled'], 'all'),
    objectiveId: params.get('objective') || 'all',
    // Malformed tuples, blank identifiers and unknown kinds normalize to all
    // without disturbing any unrelated coordinate.
    outcomeFilter: decodeOutcomeFilter(params.get('outcome')),
    taskId: params.get('task') || null,
    captureId: params.get('capture') || null,
    doneVisibility: normalizeDoneVisibility(suppliedVisibility ?? undefined, legacyExplicitAll),
  })
}

export function normalizeUrlState(state: AppUrlState): NormalizedAppUrlState {
  const taskId = state.surface === 'inbox' ? null : state.taskId
  const captureId = state.surface === 'inbox' ? state.captureId : null
  // No legacy flag here: an in-memory status of 'all' must never be read as an
  // old explicit All. Only readUrlState sees literal parameter presence.
  const doneVisibility = normalizeDoneVisibility(state.doneVisibility)
  const outcomeFilter = normalizeOutcomeFilter(state.outcomeFilter ?? { kind: 'all' })
  if (
    taskId === state.taskId
    && captureId === state.captureId
    && doneVisibility === state.doneVisibility
    && outcomeFilter === state.outcomeFilter
  ) {
    return state as NormalizedAppUrlState
  }
  return { ...state, taskId, captureId, doneVisibility, outcomeFilter }
}

export function writeUrlState(state: AppUrlState, replace: boolean): NormalizedAppUrlState {
  const normalized = normalizeUrlState(state)
  const params = new URLSearchParams()
  if (normalized.surface !== defaultState.surface) params.set('surface', normalized.surface)
  if (normalized.view !== defaultState.view) params.set('view', normalized.view)
  if (normalized.search) params.set('q', normalized.search)
  if (normalized.status !== 'all') params.set('status', normalized.status)
  if (normalized.priority !== 'all') params.set('priority', normalized.priority)
  if (normalized.readiness !== 'all') params.set('readiness', normalized.readiness)
  if (normalized.timing !== 'all') params.set('timing', normalized.timing)
  if (normalized.objectiveId !== 'all') params.set('objective', normalized.objectiveId)
  // 'all' is omitted; 'unassigned' is literal; a pair is the JSON tuple, which
  // URLSearchParams round-trips exactly even for delimiter-bearing identifiers.
  const outcome = encodeOutcomeFilter(normalized.outcomeFilter)
  if (outcome !== null) params.set('outcome', outcome)
  if (normalized.taskId) params.set('task', normalized.taskId)
  if (normalized.captureId) params.set('capture', normalized.captureId)
  // 'default' is omitted exactly as 'all' statuses are; hide/show serialize.
  if (normalized.doneVisibility !== 'default') {
    params.set('doneVisibility', normalized.doneVisibility)
  }

  const nextUrl = `${window.location.pathname}${params.size ? `?${params}` : ''}${window.location.hash}`
  window.history[replace ? 'replaceState' : 'pushState'](null, '', nextUrl)
  return normalized
}

export type UrlNavigationGuard = (
  current: NormalizedAppUrlState,
  next: NormalizedAppUrlState,
) => boolean

export function useUrlState(navigationGuard?: UrlNavigationGuard) {
  const [state, setState] = useState<NormalizedAppUrlState>(() => readUrlState())
  const stateRef = useRef(state)
  const navigationGuardRef = useRef(navigationGuard)
  stateRef.current = state
  navigationGuardRef.current = navigationGuard

  useEffect(() => {
    // Normalize the current history entry as well as in-memory state so stale
    // task/capture drawer parameters cannot reappear after a refresh or popstate.
    writeUrlState(readUrlState(), true)
    const onPopState = () => {
      const next = readUrlState()
      const current = stateRef.current
      if (navigationGuardRef.current && !navigationGuardRef.current(current, next)) {
        writeUrlState(current, true)
        return
      }
      writeUrlState(next, true)
      stateRef.current = next
      setState(next)
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const update = useCallback(
    (patch: Partial<AppUrlState>, options: { replace?: boolean } = {}) => {
      const current = stateRef.current
      const next = normalizeUrlState({ ...current, ...patch })
      if (navigationGuardRef.current && !navigationGuardRef.current(current, next)) return false
      const written = writeUrlState(next, options.replace ?? false)
      stateRef.current = written
      setState(written)
      return true
    },
    [],
  )

  return { state, update }
}
