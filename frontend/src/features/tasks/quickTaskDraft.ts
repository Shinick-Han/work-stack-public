import { z } from 'zod'
import type { QuickTaskInput } from '../../domain/types'

export const QUICK_TASK_DRAFT_KEY = 'workstack:quick-task-draft:v1'

const quickTaskDraftSchema = z.object({
  detail: z.string().max(4096),
  due: z.string().max(10),
  scheduled: z.string().max(10).default(''),
  estimateMinutes: z.string().max(4).default(''),
  objectiveId: z.string().max(80),
  priority: z.enum(['P0', 'P1', 'P2', 'P3']),
  tags: z.string().max(1000),
  title: z.string().max(240),
}).strict()

export type QuickTaskDraft = z.infer<typeof quickTaskDraftSchema>

export const EMPTY_QUICK_TASK_DRAFT: QuickTaskDraft = {
  detail: '',
  due: '',
  scheduled: '',
  estimateMinutes: '',
  objectiveId: '',
  priority: 'P2',
  tags: '',
  title: '',
}

export function readQuickTaskDraft(): QuickTaskDraft {
  if (typeof window === 'undefined') return EMPTY_QUICK_TASK_DRAFT
  try {
    const value = window.localStorage.getItem(QUICK_TASK_DRAFT_KEY)
    return value ? quickTaskDraftSchema.parse(JSON.parse(value)) : EMPTY_QUICK_TASK_DRAFT
  } catch {
    window.localStorage.removeItem(QUICK_TASK_DRAFT_KEY)
    return EMPTY_QUICK_TASK_DRAFT
  }
}

export function writeQuickTaskDraft(draft: QuickTaskDraft): void {
  if (typeof window === 'undefined') return
  const safeDraft = quickTaskDraftSchema.parse(draft)
  const empty = Object.entries(safeDraft).every(([key, value]) => (
    key === 'priority' ? value === 'P2' : value === ''
  ))
  if (empty) window.localStorage.removeItem(QUICK_TASK_DRAFT_KEY)
  else window.localStorage.setItem(QUICK_TASK_DRAFT_KEY, JSON.stringify(safeDraft))
}

export function clearQuickTaskDraft(): void {
  if (typeof window !== 'undefined') window.localStorage.removeItem(QUICK_TASK_DRAFT_KEY)
}

export function draftToInput(draft: QuickTaskDraft): QuickTaskInput {
  return {
    title: draft.title.trim(),
    detail: draft.detail.trim() || undefined,
    priority: draft.priority,
    due: draft.due || null,
    scheduled: draft.scheduled || null,
    estimate_minutes: draft.estimateMinutes ? Number(draft.estimateMinutes) : null,
    objective_ids: draft.objectiveId ? [draft.objectiveId] : [],
    tags: draft.tags.split(',').map((tag) => tag.trim()).filter(Boolean),
  }
}
