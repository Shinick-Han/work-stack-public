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
  outcomeFilter: { kind: 'all' },
  taskId: null,
  captureId: null,
  doneVisibility: 'default',
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

describe('durable doneVisibility coordinate', () => {
  test('a fresh URL with no status parameter stays default', () => {
    expect(readUrlState('').doneVisibility).toBe('default')
    expect(readUrlState('?view=board').doneVisibility).toBe('default')
  })

  test('a literal legacy status=all becomes a durable show', () => {
    expect(readUrlState('?status=all').doneVisibility).toBe('show')
    expect(readUrlState('?view=table&status=all').doneVisibility).toBe('show')
  })

  test('an invalid status value alone never implies show', () => {
    // 'bogus' falls back to status 'all', but the literal parameter was not
    // 'all', so the legacy rule must not fire.
    const read = readUrlState('?status=bogus')
    expect(read.status).toBe('all')
    expect(read.doneVisibility).toBe('default')
  })

  test('an explicit Done status keeps the coordinate untouched', () => {
    expect(readUrlState('?status=done')).toMatchObject({
      status: 'done',
      doneVisibility: 'default',
    })
    expect(readUrlState('?status=done&doneVisibility=hide')).toMatchObject({
      status: 'done',
      doneVisibility: 'hide',
    })
  })

  test('a supplied coordinate wins over the legacy status rule', () => {
    expect(readUrlState('?status=all&doneVisibility=default').doneVisibility).toBe('default')
    expect(readUrlState('?status=all&doneVisibility=hide').doneVisibility).toBe('hide')
    expect(readUrlState('?status=all&doneVisibility=show').doneVisibility).toBe('show')
  })

  test('an invalid or empty supplied coordinate normalizes to default', () => {
    expect(readUrlState('?doneVisibility=nonsense').doneVisibility).toBe('default')
    expect(readUrlState('?doneVisibility=').doneVisibility).toBe('default')
    // Presence with a bad value must not be mistaken for absence, so even with
    // a legacy status=all present the supplied-but-invalid value wins.
    expect(readUrlState('?status=all&doneVisibility=nonsense').doneVisibility).toBe('default')
  })

  test('serializes hide and show but omits default', () => {
    window.history.replaceState(null, '', '/')
    writeUrlState(state({ doneVisibility: 'show' }), false)
    expect(window.location.search).toBe('?doneVisibility=show')

    window.history.replaceState(null, '', '/')
    writeUrlState(state({ doneVisibility: 'hide' }), false)
    expect(window.location.search).toBe('?doneVisibility=hide')

    window.history.replaceState(null, '', '/')
    writeUrlState(state({ doneVisibility: 'default' }), false)
    expect(window.location.search).toBe('')
  })

  test('canonicalizes a legacy ?status=all link and keeps show on reload', () => {
    window.history.replaceState(null, '', '/?status=all')
    const first = readUrlState()
    expect(first.doneVisibility).toBe('show')

    writeUrlState(first, true)
    expect(window.location.search).toBe('?doneVisibility=show')

    // Repeated read/write cycles must not drift back to default.
    const second = readUrlState()
    expect(second.doneVisibility).toBe('show')
    writeUrlState(second, true)
    expect(window.location.search).toBe('?doneVisibility=show')
    expect(readUrlState().doneVisibility).toBe('show')
  })

  test('read, write and read again agree for every coordinate value', () => {
    for (const value of ['default', 'hide', 'show'] as const) {
      window.history.replaceState(null, '', '/')
      const written = writeUrlState(state({ doneVisibility: value }), true)
      expect(written.doneVisibility).toBe(value)
      expect(readUrlState().doneVisibility).toBe(value)
    }
  })

  test('in-memory status=all alone does not infer a legacy show', () => {
    // Programmatic state, not a parsed URL: the legacy rule is URL-only.
    expect(normalizeUrlState(state({ status: 'all' })).doneVisibility).toBe('default')

    const withoutCoordinate = { ...state(), doneVisibility: undefined }
    expect(normalizeUrlState(withoutCoordinate).doneVisibility).toBe('default')

    window.history.replaceState(null, '', '/')
    writeUrlState(withoutCoordinate, false)
    expect(window.location.search).toBe('')
  })

  test('normalizeUrlState always yields a concrete coordinate', () => {
    expect(normalizeUrlState({ ...state(), doneVisibility: undefined }).doneVisibility)
      .toBe('default')
    expect(
      normalizeUrlState({ ...state(), doneVisibility: 'bogus' as never }).doneVisibility,
    ).toBe('default')
    expect(normalizeUrlState(state({ doneVisibility: 'show' })).doneVisibility).toBe('show')
  })

  test('preserves pathname, hash and the task/capture invariant alongside the coordinate', () => {
    window.history.replaceState(null, '', '/work-stack?old=1#focus')
    const normalized = writeUrlState(
      state({ surface: 'focus', taskId: 'T-0100', captureId: 'C-0100', doneVisibility: 'show' }),
      false,
    )

    expect(normalized.captureId).toBeNull()
    expect(normalized.doneVisibility).toBe('show')
    expect(window.location.pathname).toBe('/work-stack')
    expect(window.location.search).toBe('?surface=focus&task=T-0100&doneVisibility=show')
    expect(window.location.hash).toBe('#focus')
  })

  test('leaves unrelated query semantics untouched', () => {
    expect(readUrlState('?doneVisibility=show&q=release&priority=P1&objective=O-2'))
      .toMatchObject({
        doneVisibility: 'show',
        search: 'release',
        priority: 'P1',
        objectiveId: 'O-2',
      })
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

  test('canonicalizes a legacy All entry point on mount and keeps it across popstate', () => {
    window.history.replaceState(null, '', '/?status=all')
    const { result } = renderHook(() => useUrlState())

    expect(result.current.state.doneVisibility).toBe('show')
    expect(window.location.search).toBe('?doneVisibility=show')

    act(() => {
      window.history.pushState(null, '', '/?doneVisibility=show&view=board')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })

    expect(result.current.state).toMatchObject({ doneVisibility: 'show', view: 'board' })
    expect(window.location.search).toBe('?view=board&doneVisibility=show')
  })

  test('updates the coordinate without disturbing the current Task drawer', () => {
    window.history.replaceState(null, '', '/?task=T-0014')
    const { result } = renderHook(() => useUrlState())

    act(() => result.current.update({ doneVisibility: 'hide' }))

    expect(result.current.state).toMatchObject({ taskId: 'T-0014', doneVisibility: 'hide' })
    expect(window.location.search).toBe('?task=T-0014&doneVisibility=hide')
  })
})


describe('outcome filter persistence', () => {
  test('all is omitted, unassigned is literal and a pair round-trips exactly', () => {
    expect(readUrlState('').outcomeFilter).toEqual({ kind: 'all' })
    expect(readUrlState('?outcome=unassigned').outcomeFilter).toEqual({ kind: 'unassigned' })

    const encoded = encodeURIComponent(JSON.stringify(['pair', 'O-A::B', 'KR|X']))
    expect(readUrlState(`?outcome=${encoded}`).outcomeFilter).toEqual({
      kind: 'pair',
      objectiveId: 'O-A::B',
      keyResultId: 'KR|X',
    })
  })

  test.each([
    ['unknown kind', 'sometimes'],
    ['malformed json', '%7Bnope'],
    ['wrong tuple length', encodeURIComponent(JSON.stringify(['pair', 'O-1']))],
    ['blank identity', encodeURIComponent(JSON.stringify(['pair', ' ', 'KR-1']))],
    ['non-string identity', encodeURIComponent(JSON.stringify(['pair', 'O-1', 7]))],
  ])('%s normalizes to all without deleting unrelated state', (_name, raw) => {
    const parsed = readUrlState(`?surface=focus&q=release&outcome=${raw}`)

    expect(parsed.outcomeFilter).toEqual({ kind: 'all' })
    expect(parsed.surface).toBe('focus')
    expect(parsed.search).toBe('release')
  })

  test('identifiers are never trimmed or uppercased on read', () => {
    const encoded = encodeURIComponent(JSON.stringify(['pair', ' o-1 ', ' kr-1 ']))

    expect(readUrlState(`?outcome=${encoded}`).outcomeFilter).toEqual({
      kind: 'pair',
      objectiveId: ' o-1 ',
      keyResultId: ' kr-1 ',
    })
  })
})
