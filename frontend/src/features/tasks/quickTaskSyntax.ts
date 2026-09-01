import type { TaskPriority } from '../../domain/types'

export interface ParsedQuickTaskSyntax {
  title: string
  priority?: TaskPriority
  objectiveId?: string
  scheduled?: string
  due?: string
  tags: string[]
  estimateMinutes?: number
}

export class QuickTaskSyntaxError extends Error {}

interface QuickTaskParseState {
  title: string[]
  tags: string[]
  priority?: TaskPriority
  objectiveId?: string
  scheduled?: string
  due?: string
  estimateMinutes?: number
}

function isoDate(value: string, field: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new QuickTaskSyntaxError(`${field} requires YYYY-MM-DD.`)
  const parsed = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new QuickTaskSyntaxError(`${field} requires a real calendar date.`)
  }
  return value
}

function consumePriority(token: string, state: QuickTaskParseState): boolean {
  if (!/^!p[0-3]$/i.test(token)) return false
  if (state.priority) throw new QuickTaskSyntaxError('Priority may appear only once.')
  state.priority = token.slice(1).toUpperCase() as TaskPriority
  return true
}

function consumeObjective(token: string, knownObjectiveIds: string[], state: QuickTaskParseState): boolean {
  if (!/^@o-\d+$/i.test(token)) return false
  if (state.objectiveId) throw new QuickTaskSyntaxError('Objective may appear only once.')
  state.objectiveId = token.slice(1).toUpperCase()
  if (!knownObjectiveIds.includes(state.objectiveId)) {
    throw new QuickTaskSyntaxError(`Unknown Objective: ${state.objectiveId}.`)
  }
  return true
}

function consumeTag(token: string, state: QuickTaskParseState): boolean {
  if (!/^#[\p{L}\p{N}._-]+$/u.test(token)) return false
  state.tags.push(token.slice(1))
  return true
}

function parseEstimate(argument: string): number {
  const match = /^(\d+)(m|h)$/.exec(argument)
  if (!match) throw new QuickTaskSyntaxError('/estimate requires minutes or hours, such as 90m or 2h.')
  const estimateMinutes = Number(match[1]) * (match[2] === 'h' ? 60 : 1)
  if (!Number.isInteger(estimateMinutes) || estimateMinutes < 1 || estimateMinutes > 1440) {
    throw new QuickTaskSyntaxError('/estimate must be between 1 minute and 24 hours.')
  }
  return estimateMinutes
}

function consumeCommand(token: string, argument: string | undefined, state: QuickTaskParseState): boolean {
  if (!token.startsWith('/')) return false
  if (!argument) throw new QuickTaskSyntaxError(`${token} requires a value.`)
  if (token === '/plan') {
    if (state.scheduled) throw new QuickTaskSyntaxError('/plan may appear only once.')
    state.scheduled = isoDate(argument, '/plan')
    return true
  }
  if (token === '/due') {
    if (state.due) throw new QuickTaskSyntaxError('/due may appear only once.')
    state.due = isoDate(argument, '/due')
    return true
  }
  if (token === '/estimate') {
    if (state.estimateMinutes) throw new QuickTaskSyntaxError('/estimate may appear only once.')
    state.estimateMinutes = parseEstimate(argument)
    return true
  }
  throw new QuickTaskSyntaxError(`Unknown shorthand command: ${token}.`)
}

function consumeToken(
  token: string,
  argument: string | undefined,
  knownObjectiveIds: string[],
  state: QuickTaskParseState,
): boolean {
  if (consumePriority(token, state)) return false
  if (consumeObjective(token, knownObjectiveIds, state)) return false
  if (consumeTag(token, state)) return false
  if (consumeCommand(token, argument, state)) return true
  if (token.startsWith('!') || token.startsWith('@') || token.startsWith('#')) {
    throw new QuickTaskSyntaxError(`Malformed shorthand token: ${token}.`)
  }
  state.title.push(token)
  return false
}

export function parseQuickTaskSyntax(value: string, knownObjectiveIds: string[]): ParsedQuickTaskSyntax {
  const tokens = value.trim().split(/\s+/).filter(Boolean)
  const state: QuickTaskParseState = { tags: [], title: [] }

  for (let index = 0; index < tokens.length; index += 1) {
    if (consumeToken(tokens[index], tokens[index + 1], knownObjectiveIds, state)) index += 1
  }

  const normalizedTitle = state.title.join(' ').trim()
  if (!normalizedTitle) throw new QuickTaskSyntaxError('A task title is required after shorthand is removed.')
  return {
    title: normalizedTitle,
    priority: state.priority,
    objectiveId: state.objectiveId,
    scheduled: state.scheduled,
    due: state.due,
    tags: [...new Set(state.tags)],
    estimateMinutes: state.estimateMinutes,
  }
}
