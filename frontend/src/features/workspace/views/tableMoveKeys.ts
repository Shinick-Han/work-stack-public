export type TableNudgeAction = 'previous' | 'next' | 'first' | 'last'

export type TableMoveGesture = {
  id: string
  source: 'pointer' | 'keyboard'
}

export type TableMoveCommand =
  | { kind: 'ignore' }
  | { kind: 'consume' }
  | { kind: 'begin' }
  | { kind: 'commit' }
  | { kind: 'cancel' }
  | { kind: 'nudge'; action: TableNudgeAction }

export function tableNudgeAction(key: string): TableNudgeAction | null {
  if (key === 'ArrowLeft' || key === 'ArrowUp') return 'previous'
  if (key === 'ArrowRight' || key === 'ArrowDown') return 'next'
  if (key === 'Home') return 'first'
  if (key === 'End') return 'last'
  return null
}

export function decodeTableMoveKey(
  key: string,
  gesture: TableMoveGesture | null,
  handleId: string,
): TableMoveCommand {
  if (key === 'Enter' || key === ' ') {
    if (gesture?.source === 'pointer') return { kind: 'consume' }
    if (!gesture || gesture.id !== handleId) return { kind: 'begin' }
    return { kind: 'commit' }
  }
  if (key === 'Escape' && gesture) return { kind: 'cancel' }
  if (!gesture || gesture.id !== handleId || gesture.source !== 'keyboard') {
    return { kind: 'ignore' }
  }
  const action = tableNudgeAction(key)
  if (!action) return { kind: 'ignore' }
  return { kind: 'nudge', action }
}
