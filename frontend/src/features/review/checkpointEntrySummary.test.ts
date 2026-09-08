import { describe, expect, test } from 'vitest'

import * as neutral from '../../domain/checkpointEntrySummary'
import * as review from './checkpointEntrySummary'

/**
 * The reader moved to `domain/` so the Task resume view can share it. Review
 * keeps its own import path through a re-export, and the point of that shim is
 * that it is a shim: one implementation, no second interpretation of what a
 * checkpoint record says. The behaviour itself is covered where it now lives,
 * in `domain/checkpointEntrySummary.test.ts`.
 */
describe('the Review import path is the neutral module', () => {
  test('every export is reachable and identical', () => {
    expect(Object.keys(review).sort()).toEqual(Object.keys(neutral).sort())
    for (const key of Object.keys(neutral) as (keyof typeof neutral)[]) {
      expect(review[key]).toBe(neutral[key])
    }
  })

  test('the shim adds no interpretation of its own', () => {
    const payload = { task_id: 'T-0007', done: ['shipped'], mystery: 1 }
    expect(review.summarizeCheckpointEntry(payload))
      .toEqual(neutral.summarizeCheckpointEntry(payload))
  })
})
