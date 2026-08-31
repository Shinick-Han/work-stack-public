import { act, renderHook } from '@testing-library/react'
import { describe, expect, test } from 'vitest'
import type { AppUrlState } from '../domain/types'
import {
  normalizeUrlState,
  readUrlState,
  useUrlState,
  writeUrlState,
} from './urlState'

const state = (patch: Partial<AppUrlState> = {}): AppUrlState => ({
  surface: 'workspace',
  view: 'graph',
  search: '',
  status: 'all',
  priority: 'all',
  readiness: 'all',
  timing: 'all',
  objectiveId: 'all',
  taskId: null,
  captureId: null,
  ...patch,
})

describe('URL state canonicalization', () => {
  test('preserves a Focus deep link and removes an incompatible capture drawer', () => {
    expect(readUrlState(
      '?surface=focus&task=T-0001&capture=C-0001&view=board&q=release&status=started&priority=P1&objective=O-1',
    )).toEqual(state({
      surface: 'focus',
      taskId: 'T-0001',
      view: 'board',
      search: 'release',
      status: 'started',
      priority: 'P1',
      objectiveId: 'O-1',
    }))
  })

  test('keeps only the drawer supported by each surface', () => {
    expect(readUrlState('?surface=workspace&task=T-0002&capture=C-0002')).toMatchObject({
      surface: 'workspace',
      taskId: 'T-0002',
      captureId: null,
    })
    expect(readUrlState('?surface=inbox&task=T-0003&capture=C-0003')).toMatchObject({
      surface: 'inbox',
      taskId: null,
      captureId: 'C-0003',
    })
    expect(readUrlState('?surface=review&task=T-0004&capture=C-0004')).toMatchObject({
      surface: 'review',
      taskId: 'T-0004',
      captureId: null,
    })
  })

  test('round-trips a readiness filter and rejects unknown readiness values', () => {
    expect(readUrlState('?view=table&readiness=blocked')).toMatchObject({
      view: 'table',
      readiness: 'blocked',
    })
    expect(readUrlState('?readiness=unknown').readiness).toBe('all')

    window.history.replaceState(null, '', '/')
    writeUrlState(state({ readiness: 'ready' }), false)
    expect(window.location.search).toBe('?readiness=ready')
  })

  test('round-trips a due-timing filter and rejects unknown values', () => {
    expect(readUrlState('?timing=overdue')).toMatchObject({ timing: 'overdue' })
    expect(readUrlState('?timing=unknown').timing).toBe('all')

    window.history.replaceState(null, '', '/')
    writeUrlState(state({ timing: 'soon' }), false)
    expect(window.location.search).toBe('?timing=soon')
  })

  test('falls an unknown surface back to Workspace before applying its invariant', () => {
    expect(readUrlState('?surface=unknown&task=T-0004&capture=C-0004')).toMatchObject({
      surface: 'workspace',
      taskId: 'T-0004',
      captureId: null,
    })
  })

  test('normalizes programmatic state without mutating its input', () => {
    const input = state({ surface: 'focus', taskId: 'T-0005', captureId: 'C-0005' })
    const normalized = normalizeUrlState(input)

    expect(normalized).toEqual(state({ surface: 'focus', taskId: 'T-0005' }))
    expect(input.captureId).toBe('C-0005')
  })

  test('canonicalizes state before writing a history entry', () => {
    window.history.replaceState(null, '', '/work-stack?old=1#focus')
    const normalized = writeUrlState(
      state({ surface: 'focus', taskId: 'T-0006', captureId: 'C-0006' }),
      false,
    )

    expect(normalized.captureId).toBeNull()
    expect(window.location.pathname).toBe('/work-stack')
    expect(window.location.search).toBe('?surface=focus&task=T-0006')
    expect(window.location.hash).toBe('#focus')
  })
})

describe('useUrlState', () => {
  test('canonicalizes surface transitions before updating state and URL', () => {
    window.history.replaceState(null, '', '/?surface=inbox&capture=C-0007')
    const { result } = renderHook(() => useUrlState())

    act(() => result.current.update({ surface: 'focus', taskId: 'T-0007' }))
    expect(result.current.state).toMatchObject({
      surface: 'focus',
      taskId: 'T-0007',
      captureId: null,
    })
    expect(window.location.search).toBe('?surface=focus&task=T-0007')

    act(() => result.current.update({ surface: 'inbox', captureId: 'C-0008' }))
    expect(result.current.state).toMatchObject({
      surface: 'inbox',
      taskId: null,
      captureId: 'C-0008',
    })
    expect(window.location.search).toBe('?surface=inbox&capture=C-0008')
  })

  test('normalizes a history entry received through popstate', () => {
    window.history.replaceState(null, '', '/?surface=workspace')
    const { result } = renderHook(() => useUrlState())

    act(() => {
      window.history.pushState(null, '', '/?surface=focus&task=T-0009&capture=C-0009')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })

    expect(result.current.state).toMatchObject({
      surface: 'focus',
      taskId: 'T-0009',
      captureId: null,
    })
    expect(window.location.search).toBe('?surface=focus&task=T-0009')
  })

  test('replaces an invalid initial deep link with its canonical URL', () => {
    window.history.replaceState(null, '', '/?surface=inbox&task=T-0010&capture=C-0010')
    const { result } = renderHook(() => useUrlState())

    expect(result.current.state).toMatchObject({
      surface: 'inbox',
      taskId: null,
      captureId: 'C-0010',
    })
    expect(window.location.search).toBe('?surface=inbox&capture=C-0010')
  })

  test('restores the current Task URL when a navigation guard rejects popstate', () => {
    window.history.replaceState(null, '', '/?view=table&task=T-0011')
    const guard = (current: AppUrlState, next: AppUrlState) => current.taskId === next.taskId
    const { result } = renderHook(() => useUrlState(guard))

    act(() => {
      window.history.pushState(null, '', '/?view=table')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })

    expect(result.current.state.taskId).toBe('T-0011')
    expect(window.location.search).toBe('?view=table&task=T-0011')
  })

  test('rejects a programmatic Task change through the same navigation guard', () => {
    window.history.replaceState(null, '', '/?task=T-0012')
    const guard = (current: AppUrlState, next: AppUrlState) => current.taskId === next.taskId
    const { result } = renderHook(() => useUrlState(guard))

    act(() => result.current.update({ taskId: 'T-0013' }))

    expect(result.current.state.taskId).toBe('T-0012')
    expect(window.location.search).toBe('?task=T-0012')
  })
})
