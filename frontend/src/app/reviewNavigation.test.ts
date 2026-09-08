import { describe, expect, test, vi } from 'vitest'
import { readUrlState } from './urlState'
import { allowDraftNavigation, reviewNavigationPatch } from './reviewNavigation'

describe('Review draft ownership at app navigation boundaries', () => {
  test('binds Record progress to the open Task instead of a previously selected Review target', () => {
    const state = readUrlState('?surface=review&task=T-0027&reviewTask=T-0033')
    expect(reviewNavigationPatch(state)).toEqual({ surface: 'review', taskId: null, captureId: null, reviewTaskId: 'T-0027' })
  })

  test('blocks route and browser-history transitions that could orphan a progress draft', () => {
    const current = readUrlState('?surface=review&reviewTask=T-0033')
    const notify = vi.fn()
    expect(allowDraftNavigation(current, readUrlState('?surface=focus'), false, true, notify)).toBe(false)
    expect(allowDraftNavigation(current, readUrlState('?surface=review&reviewTask=T-0027'), false, true, notify)).toBe(false)
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('progress draft'), 'error')
  })

  test('lets a Review draft inspect another Task without changing its write target', () => {
    const current = readUrlState('?surface=review&reviewTask=T-0033')
    const next = readUrlState('?surface=review&reviewTask=T-0033&task=T-0027')
    const notify = vi.fn()
    expect(allowDraftNavigation(current, next, false, true, notify)).toBe(true)
    expect(notify).not.toHaveBeenCalled()
    expect(allowDraftNavigation(next, { ...next, taskId: null }, true, true, notify)).toBe(false)
  })

  test('releases the navigation lock after the form has saved or explicitly discarded its draft', () => {
    const current = readUrlState('?surface=review&reviewTask=T-0033')
    expect(allowDraftNavigation(current, readUrlState('?surface=workspace'), false, false, vi.fn())).toBe(true)
    expect(reviewNavigationPatch(readUrlState('?surface=workspace')).reviewTaskId).toBeUndefined()
  })
})
