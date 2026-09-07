import type { AppUrlState } from '../../domain/types'
import { statusLabels } from '../../utils/format'

export const viewMeta = {
  graph: { label: 'Graph', icon: 'graph' as const, description: 'See alignment and dependencies' },
  board: { label: 'Board', icon: 'board' as const, description: 'Move work through its flow' },
  treemap: { label: 'Treemap', icon: 'treemap' as const, description: 'Read effort by objective' },
  table: { label: 'Table', icon: 'table' as const, description: 'Scan and sort planning facts' },
}

export const readinessLabels = {
  all: 'All readiness',
  ready: 'Ready to act',
  blocked: 'Blocked work',
} as const

export const timingLabels = {
  all: 'All due timing',
  overdue: 'Overdue',
  today: 'Due today',
  soon: 'Due soon',
  unscheduled: 'No due date',
} as const

export function suggestedSavedViewName(state: AppUrlState): string {
  const parts = [
    state.search ? `“${state.search.slice(0, 36)}”` : '',
    state.status !== 'all' ? statusLabels[state.status] : '',
    state.priority !== 'all' ? state.priority : '',
    state.objectiveId !== 'all' ? state.objectiveId : '',
    state.readiness !== 'all' ? readinessLabels[state.readiness] : '',
    state.timing !== 'all' ? timingLabels[state.timing] : '',
    viewMeta[state.view].label,
  ].filter(Boolean)
  return parts.join(' · ') || 'All Tasks'
}
