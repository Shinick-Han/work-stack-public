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

function isoDate(value: string, field: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new QuickTaskSyntaxError(`${field} requires YYYY-MM-DD.`)
  const parsed = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new QuickTaskSyntaxError(`${field} requires a real calendar date.`)
  }
  return value
}

export function parseQuickTaskSyntax(value: string, knownObjectiveIds: string[]): ParsedQuickTaskSyntax {
  const tokens = value.trim().split(/\s+/).filter(Boolean)
  const title: string[] = []
  const tags: string[] = []
  let priority: TaskPriority | undefined
  let objectiveId: string | undefined
  let scheduled: string | undefined
  let due: string | undefined
  let estimateMinutes: number | undefined

  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index]
    if (/^!p[0-3]$/i.test(token)) {
      if (priority) throw new QuickTaskSyntaxError('Priority may appear only once.')
      priority = token.slice(1).toUpperCase() as TaskPriority
      continue
    }
    if (/^@o-\d+$/i.test(token)) {
      if (objectiveId) throw new QuickTaskSyntaxError('Objective may appear only once.')
      objectiveId = token.slice(1).toUpperCase()
      if (!knownObjectiveIds.includes(objectiveId)) throw new QuickTaskSyntaxError(`Unknown Objective: ${objectiveId}.`)
      continue
    }
    if (/^#[\p{L}\p{N}._-]+$/u.test(token)) {
      tags.push(token.slice(1))
      continue
    }
    if (token.startsWith('/')) {
      const argument = tokens[index + 1]
      if (!argument) throw new QuickTaskSyntaxError(`${token} requires a value.`)
      if (token === '/plan') {
        if (scheduled) throw new QuickTaskSyntaxError('/plan may appear only once.')
        scheduled = isoDate(argument, '/plan')
      } else if (token === '/due') {
        if (due) throw new QuickTaskSyntaxError('/due may appear only once.')
        due = isoDate(argument, '/due')
      } else if (token === '/estimate') {
        if (estimateMinutes) throw new QuickTaskSyntaxError('/estimate may appear only once.')
        const match = /^(\d+)(m|h)$/.exec(argument)
        if (!match) throw new QuickTaskSyntaxError('/estimate requires minutes or hours, such as 90m or 2h.')
        estimateMinutes = Number(match[1]) * (match[2] === 'h' ? 60 : 1)
        if (!Number.isInteger(estimateMinutes) || estimateMinutes < 1 || estimateMinutes > 1440) {
          throw new QuickTaskSyntaxError('/estimate must be between 1 minute and 24 hours.')
        }
      } else {
        throw new QuickTaskSyntaxError(`Unknown shorthand command: ${token}.`)
      }
      index += 1
      continue
    }
    if (token.startsWith('!') || token.startsWith('@') || token.startsWith('#')) {
      throw new QuickTaskSyntaxError(`Malformed shorthand token: ${token}.`)
    }
    title.push(token)
  }

  const normalizedTitle = title.join(' ').trim()
  if (!normalizedTitle) throw new QuickTaskSyntaxError('A task title is required after shorthand is removed.')
  return {
    title: normalizedTitle,
    priority,
    objectiveId,
    scheduled,
    due,
    tags: [...new Set(tags)],
    estimateMinutes,
  }
}
