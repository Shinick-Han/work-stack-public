import type { AppUrlState } from '../domain/types'

/** Opening Review carries an explicit target, without leaving its drawer open. */
export function reviewNavigationPatch(state: AppUrlState): Partial<AppUrlState> {
  return { surface: 'review', reviewTaskId: state.taskId ?? state.reviewTaskId, taskId: null, captureId: null }
}

export function allowDraftNavigation(
  current: AppUrlState,
  next: AppUrlState,
  taskLocked: boolean,
  reviewLocked: boolean,
  notify: (message: string, tone: 'error') => void,
): boolean {
  if (reviewLocked && current.surface === 'review'
    && (next.surface !== 'review' || next.reviewTaskId !== current.reviewTaskId)) {
    notify('Save or discard your progress draft before leaving. Resolve any pending save first.', 'error')
    return false
  }
  if (taskLocked && current.taskId && next.taskId !== current.taskId) {
    notify('Finish saving or discard the unsaved Task changes before leaving.', 'error')
    return false
  }
  return true
}
