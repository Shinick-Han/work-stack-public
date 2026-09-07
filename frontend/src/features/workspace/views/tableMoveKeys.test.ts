import { describe, expect, it } from 'vitest'
import { decodeTableMoveKey, tableNudgeAction } from './tableMoveKeys'

describe('tableNudgeAction', () => {
  it('maps arrows, Home, and End onto visible-slot moves', () => {
    expect(tableNudgeAction('ArrowLeft')).toBe('previous')
    expect(tableNudgeAction('ArrowUp')).toBe('previous')
    expect(tableNudgeAction('ArrowRight')).toBe('next')
    expect(tableNudgeAction('ArrowDown')).toBe('next')
    expect(tableNudgeAction('Home')).toBe('first')
    expect(tableNudgeAction('End')).toBe('last')
    expect(tableNudgeAction('Escape')).toBeNull()
    expect(tableNudgeAction('Enter')).toBeNull()
  })
})

describe('decodeTableMoveKey', () => {
  const keyboard = { id: 'T-A', source: 'keyboard' as const }
  const pointer = { id: 'T-A', source: 'pointer' as const }

  it('toggles pickup and drop on Enter or Space', () => {
    expect(decodeTableMoveKey('Enter', null, 'T-A')).toEqual({ kind: 'begin' })
    expect(decodeTableMoveKey(' ', { id: 'T-B', source: 'keyboard' }, 'T-A')).toEqual({ kind: 'begin' })
    expect(decodeTableMoveKey('Enter', keyboard, 'T-A')).toEqual({ kind: 'commit' })
    expect(decodeTableMoveKey(' ', pointer, 'T-A')).toEqual({ kind: 'consume' })
  })

  it('cancels an active gesture on Escape and ignores idle Escape', () => {
    expect(decodeTableMoveKey('Escape', keyboard, 'T-A')).toEqual({ kind: 'cancel' })
    expect(decodeTableMoveKey('Escape', pointer, 'T-A')).toEqual({ kind: 'cancel' })
    expect(decodeTableMoveKey('Escape', null, 'T-A')).toEqual({ kind: 'ignore' })
  })

  it('nudges only the matching keyboard gesture', () => {
    expect(decodeTableMoveKey('ArrowDown', keyboard, 'T-A')).toEqual({ kind: 'nudge', action: 'next' })
    expect(decodeTableMoveKey('Home', keyboard, 'T-A')).toEqual({ kind: 'nudge', action: 'first' })
    expect(decodeTableMoveKey('ArrowDown', pointer, 'T-A')).toEqual({ kind: 'ignore' })
    expect(decodeTableMoveKey('ArrowDown', keyboard, 'T-B')).toEqual({ kind: 'ignore' })
    expect(decodeTableMoveKey('ArrowDown', null, 'T-A')).toEqual({ kind: 'ignore' })
    expect(decodeTableMoveKey('Tab', keyboard, 'T-A')).toEqual({ kind: 'ignore' })
  })
})
