import { beforeEach, expect, test } from 'vitest'
import {
  EMPTY_QUICK_TASK_DRAFT,
  QUICK_TASK_DRAFT_KEY,
  clearQuickTaskDraft,
  readQuickTaskDraft,
  writeQuickTaskDraft,
} from './quickTaskDraft'

beforeEach(() => window.localStorage.clear())

test('round-trips only the bounded planning draft fields', () => {
  const draft = { ...EMPTY_QUICK_TASK_DRAFT, title: 'Plan release', detail: 'Define done', priority: 'P1' as const }
  writeQuickTaskDraft(draft)
  expect(readQuickTaskDraft()).toEqual(draft)
  expect(Object.keys(JSON.parse(window.localStorage.getItem(QUICK_TASK_DRAFT_KEY) ?? '{}')).sort()).toEqual([
    'detail', 'due', 'estimateMinutes', 'objectiveId', 'priority', 'scheduled', 'tags', 'title',
  ])
  clearQuickTaskDraft()
  expect(readQuickTaskDraft()).toEqual(EMPTY_QUICK_TASK_DRAFT)
})

test('fails closed and removes malformed persisted drafts', () => {
  window.localStorage.setItem(QUICK_TASK_DRAFT_KEY, JSON.stringify({ title: 'partial', token: 'must-not-survive' }))
  expect(readQuickTaskDraft()).toEqual(EMPTY_QUICK_TASK_DRAFT)
  expect(window.localStorage.getItem(QUICK_TASK_DRAFT_KEY)).toBeNull()
})

test('upgrades a valid pre-scheduling draft without losing its content', () => {
  window.localStorage.setItem(QUICK_TASK_DRAFT_KEY, JSON.stringify({
    detail: 'Keep this definition',
    due: '2026-09-04',
    objectiveId: 'O-1',
    priority: 'P1',
    tags: 'report',
    title: 'Keep this title',
  }))
  expect(readQuickTaskDraft()).toEqual({
    detail: 'Keep this definition',
    due: '2026-09-04',
    estimateMinutes: '',
    objectiveId: 'O-1',
    priority: 'P1',
    scheduled: '',
    tags: 'report',
    title: 'Keep this title',
  })
})
