import type { TaskPriority, TaskStatus } from '../domain/types'

export const statusLabels: Record<TaskStatus, string> = {
  open: 'Open',
  started: 'In progress',
  done: 'Done',
  dropped: 'Dropped',
}

export const priorityLabels: Record<TaskPriority, string> = {
  P0: 'Critical',
  P1: 'High',
  P2: 'Medium',
  P3: 'Low',
}

export function formatDate(value?: string | null, fallback = 'No due date') {
  if (!value) return fallback
  const parsed = new Date(value.length === 10 ? `${value}T12:00:00` : value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    year: parsed.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined,
  }).format(parsed)
}

export function formatDateTime(value?: string | null) {
  if (!value) return 'Unknown time'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(parsed)
}

export function getObjectiveTitle(objective: { objective: string; title?: string }) {
  return objective.objective || objective.title || 'Untitled objective'
}

export function getErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : 'Something unexpected happened.'
}

export function safeExternalUrl(value?: string | null) {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' ? url.toString() : null
  } catch {
    return null
  }
}
