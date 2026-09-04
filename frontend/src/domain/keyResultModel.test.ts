import { describe, expect, it } from 'vitest'

import * as domainModel from './keyResultModel'
import * as featureModel from '../features/workspace/views/keyResultModel'
import { DONE_VISIBILITIES } from './workspaceFilterTypes'
import {
  DONE_VISIBILITIES as featureDoneVisibilities,
  normalizeDoneVisibility,
} from '../features/workspace/views/completedTaskVisibility'
import type { Objective, Task } from './types'

/**
 * Q3 acceptance: the pure Key Result implementation MOVED here. The feature
 * path stays a compatibility surface, so every public runtime export must be
 * the very same function, and the shared filter constant must be one array
 * instance rather than a copy.
 */

describe('the moved implementation is the one the old path exports', () => {
  it('exports the identical runtime function for every public name', () => {
    const runtimeNames = Object.keys(domainModel).filter(
      (name) => typeof (domainModel as Record<string, unknown>)[name] === 'function',
    )

    expect(runtimeNames.length).toBeGreaterThan(0)
    for (const name of runtimeNames) {
      expect((featureModel as Record<string, unknown>)[name]).toBe(
        (domainModel as Record<string, unknown>)[name],
      )
    }
    // The compatibility surface adds nothing of its own.
    expect(Object.keys(featureModel).sort()).toEqual(Object.keys(domainModel).sort())
  })

  it('keeps DONE_VISIBILITIES a single shared array and the old normalizer unchanged', () => {
    expect(featureDoneVisibilities).toBe(DONE_VISIBILITIES)
    expect([...DONE_VISIBILITIES]).toEqual(['default', 'hide', 'show'])
    expect(normalizeDoneVisibility('hide')).toBe('hide')
    expect(normalizeDoneVisibility('show')).toBe('show')
    expect(normalizeDoneVisibility('nonsense')).toBe('default')
    expect(normalizeDoneVisibility(undefined)).toBe('default')
  })
})

describe('the moved projection keeps its admitted semantics', () => {
  const objective = (id: string, keyResults: { id: string; text: string; progress?: number }[]) =>
    ({ id, objective: `${id} objective`, revision: 1, key_results: keyResults }) as unknown as Objective
  const task = (id: string, patch: Record<string, unknown> = {}) =>
    ({
      id,
      title: `Task ${id}`,
      status: 'open',
      priority: 'P2',
      objective_ids: [],
      dependencies: [],
      subtasks: [],
      revision: 1,
      ...patch,
    }) as unknown as Task

  it('scopes duplicate local key-result IDs under their own Objectives', () => {
    const objectives = [
      objective('O-A', [{ id: 'K1', text: 'A outcome' }]),
      objective('O-B', [{ id: 'K1', text: 'B outcome' }]),
    ]
    const tasks = [
      task('T-1', { objective_ids: ['O-A'], key_result_refs: [{ objective_id: 'O-A', key_result_id: 'K1' }] }),
      task('T-2', { objective_ids: ['O-B'], key_result_refs: [{ objective_id: 'O-B', key_result_id: 'K1' }] }),
    ]

    const projection = domainModel.projectKeyResults({ workspaceId: 'W1', tasks, objectives })
    const keys = projection.keyResults.map((node) => node.key)

    expect(new Set(keys).size).toBe(keys.length)
    expect(keys).toContain(domainModel.keyResultKey('W1', 'O-A', 'K1'))
    expect(keys).toContain(domainModel.keyResultKey('W1', 'O-B', 'K1'))
    for (const node of projection.keyResults) {
      expect(node.counts.total).toBe(1)
    }
  })

  it('classifies an unknown reference and an ambiguous parent without inventing a node', () => {
    const objectives = [objective('O-A', [{ id: 'K1', text: 'A outcome' }])]
    const tasks = [
      task('T-1', { objective_ids: ['O-A'], key_result_refs: [{ objective_id: 'O-A', key_result_id: 'K9' }] }),
      task('T-2', { objective_ids: ['O-A', 'O-B'] }),
    ]

    const projection = domainModel.projectKeyResults({ workspaceId: 'W1', tasks, objectives })
    const reasons = projection.tasks
      .flatMap((entry) => entry.refs)
      .map((ref) => (ref.resolved ? 'resolved' : ref.reason))

    expect(reasons).toContain('missing-key-result')
    expect(projection.keyResults.map((node) => node.keyResultId)).toEqual(['K1'])
  })

  it('keeps a zero-linked key result visible and leaves unrecorded progress null', () => {
    const objectives = [
      objective('O-A', [
        { id: 'K1', text: 'Never linked' },
        { id: 'K2', text: 'Recorded', progress: 40 },
      ]),
    ]

    const projection = domainModel.projectKeyResults({ workspaceId: 'W1', tasks: [], objectives })
    const byId = new Map(projection.keyResults.map((node) => [node.keyResultId, node]))

    expect(byId.get('K1')?.counts.total).toBe(0)
    expect(byId.get('K1')?.recordedProgress).toBeNull()
    expect(byId.get('K2')?.recordedProgress).toBe(40)
  })

  it('filters exactly by pair and by unassigned through the shared filter union', () => {
    const linked = task('T-1', {
      objective_ids: ['O-A'],
      key_result_refs: [{ objective_id: 'O-A', key_result_id: 'K1' }],
    })
    const unassigned = task('T-2')

    const pair = domainModel.normalizeOutcomeFilter({
      kind: 'pair',
      objectiveId: 'O-A',
      keyResultId: 'K1',
    })
    expect(domainModel.matchesOutcomeFilter(linked, pair)).toBe(true)
    expect(domainModel.matchesOutcomeFilter(unassigned, pair)).toBe(false)

    const none = domainModel.normalizeOutcomeFilter({ kind: 'unassigned' })
    expect(domainModel.matchesOutcomeFilter(unassigned, none)).toBe(true)
    expect(domainModel.matchesOutcomeFilter(linked, none)).toBe(false)

    // Legacy and malformed values still normalize to the whole workspace.
    expect(domainModel.normalizeOutcomeFilter('nonsense')).toEqual({ kind: 'all' })
    expect(domainModel.normalizeOutcomeFilter(undefined)).toEqual({ kind: 'all' })
    expect(domainModel.decodeOutcomeFilter(domainModel.encodeOutcomeFilter(pair))).toEqual(pair)
  })
})
