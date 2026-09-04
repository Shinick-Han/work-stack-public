import type { Objective, Task, TaskKeyResultRef } from './types'
import type { OutcomeFilter } from './workspaceFilterTypes'

// Q3: the filter union itself is dependency-free and shared; it is
// re-exported here so this module's public API is unchanged.
export type { OutcomeFilter }

/**
 * Pure Workspace Key Result projection.
 *
 * No React, DOM, storage, transport, clock or mutable global state is used, and
 * no second filter is applied: the caller supplies the already-filtered visible
 * Task subset and already-projected statuses.
 */

export type OutcomePlacement =
  | 'single-outcome'
  | 'multiple-outcomes'
  | 'unassigned-outcome'
  | 'unresolved-outcome'

export type UnresolvedReason =
  | 'unaligned-parent'
  | 'missing-objective'
  | 'ambiguous-objective'
  | 'missing-key-result'
  | 'ambiguous-key-result'

export interface KeyResultCounts {
  total: number
  open: number
  started: number
  done: number
  dropped: number
}

export interface KeyResultNode {
  key: string
  workspaceId: string
  objectiveId: string
  keyResultId: string
  objectiveTitle: string
  objectiveStatus: string | null
  objectiveRevision: number
  text: string
  target: string | null
  status: string | null
  /** The recorded value, or null when the workspace never recorded one. */
  recordedProgress: number | null
  linkedTaskIds: string[]
  counts: KeyResultCounts
  visibleTaskIds: string[]
  visibleCounts: KeyResultCounts
}

export interface ResolvedTaskRef {
  key: string
  ref: TaskKeyResultRef
  resolved: true
}

export interface UnresolvedTaskRef {
  key: null
  ref: TaskKeyResultRef
  resolved: false
  reason: UnresolvedReason
}

export type TaskRefResolution = ResolvedTaskRef | UnresolvedTaskRef

export interface TaskOutcomeProjection {
  taskId: string
  refs: TaskRefResolution[]
  resolvedKeys: string[]
  unresolvedRefs: TaskKeyResultRef[]
  placement: OutcomePlacement
}

export interface KeyResultProjectionInput {
  workspaceId: string
  tasks: readonly Task[]
  visibleTasks?: readonly Task[]
  objectives: readonly Objective[]
}

export interface KeyResultProjection {
  workspaceId: string
  keyResults: KeyResultNode[]
  byKey: Record<string, KeyResultNode>
  tasks: TaskOutcomeProjection[]
  unassignedTaskIds: string[]
}

const COUNTED_STATUSES = ['open', 'started', 'done', 'dropped'] as const

function encodeSegment(value: string) {
  return value.replace(/\\/g, '\\\\').replace(/\|/g, '\\|')
}

/** Unambiguous tuple key: workspace, entity kind, Objective ID, local KR ID. */
export function keyResultKey(workspaceId: string, objectiveId: string, keyResultId: string) {
  return ['ws', workspaceId, 'kr', objectiveId, keyResultId].map(encodeSegment).join('|')
}

export function refKey(workspaceId: string, ref: TaskKeyResultRef) {
  return keyResultKey(workspaceId, ref.objective_id, ref.key_result_id)
}

function emptyCounts(): KeyResultCounts {
  return { total: 0, open: 0, started: 0, done: 0, dropped: 0 }
}

function countTask(counts: KeyResultCounts, status: string) {
  counts.total += 1
  if ((COUNTED_STATUSES as readonly string[]).includes(status)) {
    counts[status as (typeof COUNTED_STATUSES)[number]] += 1
  }
}

function groupObjectives(objectives: readonly Objective[]) {
  const grouped = new Map<string, Objective[]>()
  for (const objective of objectives) {
    const bucket = grouped.get(objective.id)
    if (bucket) bucket.push(objective)
    else grouped.set(objective.id, [objective])
  }
  return grouped
}

function resolveRef(
  workspaceId: string,
  ref: TaskKeyResultRef,
  objectivesById: Map<string, Objective[]>,
  alignedObjectiveIds: ReadonlySet<string>,
): TaskRefResolution {
  if (!alignedObjectiveIds.has(ref.objective_id)) {
    return { key: null, ref, resolved: false, reason: 'unaligned-parent' }
  }
  const candidates = objectivesById.get(ref.objective_id)
  if (!candidates || candidates.length === 0) {
    return { key: null, ref, resolved: false, reason: 'missing-objective' }
  }
  if (candidates.length > 1) {
    return { key: null, ref, resolved: false, reason: 'ambiguous-objective' }
  }
  const matches = (candidates[0].key_results ?? []).filter((item) => item.id === ref.key_result_id)
  if (matches.length === 0) {
    return { key: null, ref, resolved: false, reason: 'missing-key-result' }
  }
  if (matches.length > 1) {
    return { key: null, ref, resolved: false, reason: 'ambiguous-key-result' }
  }
  return { key: refKey(workspaceId, ref), ref, resolved: true }
}

function placementFor(refs: TaskRefResolution[]): OutcomePlacement {
  if (refs.length === 0) return 'unassigned-outcome'
  if (refs.some((item) => !item.resolved)) return 'unresolved-outcome'
  return refs.length === 1 ? 'single-outcome' : 'multiple-outcomes'
}

function buildNodes(input: KeyResultProjectionInput, objectivesById: Map<string, Objective[]>) {
  const nodes: KeyResultNode[] = []
  for (const [objectiveId, records] of objectivesById) {
    if (records.length !== 1) continue
    const objective = records[0]
    const seen = new Map<string, number>()
    for (const keyResult of objective.key_results ?? []) {
      seen.set(keyResult.id, (seen.get(keyResult.id) ?? 0) + 1)
    }
    for (const keyResult of objective.key_results ?? []) {
      if (seen.get(keyResult.id) !== 1) continue
      nodes.push({
        key: keyResultKey(input.workspaceId, objectiveId, keyResult.id),
        workspaceId: input.workspaceId,
        objectiveId,
        keyResultId: keyResult.id,
        objectiveTitle: objective.title ?? objective.objective,
        objectiveStatus: objective.status ?? null,
        objectiveRevision: objective.revision,
        text: keyResult.text,
        target: keyResult.target ?? null,
        status: keyResult.status ?? null,
        recordedProgress: typeof keyResult.progress === 'number' ? keyResult.progress : null,
        linkedTaskIds: [],
        counts: emptyCounts(),
        visibleTaskIds: [],
        visibleCounts: emptyCounts(),
      })
    }
  }
  return nodes
}

export function projectKeyResults(input: KeyResultProjectionInput): KeyResultProjection {
  const objectivesById = groupObjectives(input.objectives)
  const nodes = buildNodes(input, objectivesById)
  const byKey: Record<string, KeyResultNode> = {}
  for (const node of nodes) byKey[node.key] = node
  const visibleIds = new Set((input.visibleTasks ?? input.tasks).map((task) => task.id))

  const tasks: TaskOutcomeProjection[] = []
  const unassignedTaskIds: string[] = []
  for (const task of input.tasks) {
    const aligned = new Set(task.objective_ids)
    const refs = (task.key_result_refs ?? []).map((ref) =>
      resolveRef(input.workspaceId, { ...ref }, objectivesById, aligned),
    )
    const resolvedKeys: string[] = []
    for (const item of refs) {
      if (!item.resolved) continue
      resolvedKeys.push(item.key)
      const node = byKey[item.key]
      if (!node || node.linkedTaskIds.includes(task.id)) continue
      node.linkedTaskIds.push(task.id)
      countTask(node.counts, task.status)
      if (visibleIds.has(task.id)) {
        node.visibleTaskIds.push(task.id)
        countTask(node.visibleCounts, task.status)
      }
    }
    const placement = placementFor(refs)
    if (placement === 'unassigned-outcome') unassignedTaskIds.push(task.id)
    tasks.push({
      taskId: task.id,
      refs,
      resolvedKeys,
      unresolvedRefs: refs.filter((item) => !item.resolved).map((item) => ({ ...item.ref })),
      placement,
    })
  }

  return { workspaceId: input.workspaceId, keyResults: nodes, byKey, tasks, unassignedTaskIds }
}

/** The structural shape the predicate actually reads, so legacy view Tasks fit. */
export interface OutcomeFilterable {
  readonly key_result_refs?: readonly TaskKeyResultRef[]
}

/** Normalize any stored or supplied coordinate; anything malformed becomes all. */
export function normalizeOutcomeFilter(value: unknown): OutcomeFilter {
  if (typeof value !== 'object' || value === null) return { kind: 'all' }
  const record = value as Record<string, unknown>
  if (record.kind === 'unassigned') return { kind: 'unassigned' }
  if (record.kind !== 'pair') return { kind: 'all' }
  const { objectiveId, keyResultId } = record
  if (typeof objectiveId !== 'string' || typeof keyResultId !== 'string') return { kind: 'all' }
  if (!objectiveId.trim() || !keyResultId.trim()) return { kind: 'all' }
  return { kind: 'pair', objectiveId, keyResultId }
}

/** URL form: all is omitted, unassigned is literal, a pair is a JSON tuple. */
export function encodeOutcomeFilter(filter: OutcomeFilter): string | null {
  const normalized = normalizeOutcomeFilter(filter)
  if (normalized.kind === 'all') return null
  if (normalized.kind === 'unassigned') return 'unassigned'
  return JSON.stringify(['pair', normalized.objectiveId, normalized.keyResultId])
}

export function decodeOutcomeFilter(raw: string | null | undefined): OutcomeFilter {
  if (raw === null || raw === undefined || raw === '') return { kind: 'all' }
  if (raw === 'unassigned') return { kind: 'unassigned' }
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return { kind: 'all' }
  }
  if (!Array.isArray(parsed) || parsed.length !== 3 || parsed[0] !== 'pair') return { kind: 'all' }
  return normalizeOutcomeFilter({
    kind: 'pair',
    objectiveId: parsed[1],
    keyResultId: parsed[2],
  })
}

/** Pure predicate over the stored references; a dangling pair still matches. */
export function matchesOutcomeFilter(task: OutcomeFilterable, filter: OutcomeFilter) {
  const refs = task.key_result_refs ?? []
  if (filter.kind === 'all') return true
  if (filter.kind === 'unassigned') return refs.length === 0
  return refs.some(
    (ref) => ref.objective_id === filter.objectiveId && ref.key_result_id === filter.keyResultId,
  )
}

/** Objectives already aligned on the draft whose KRs may be selected. */
export function eligibleKeyResults(objectiveIds: readonly string[], objectives: readonly Objective[]) {
  const aligned = new Set(objectiveIds)
  const projection = projectKeyResults({
    workspaceId: '',
    tasks: [],
    objectives: objectives.filter((objective) => aligned.has(objective.id)),
  })
  return projection.keyResults
}
