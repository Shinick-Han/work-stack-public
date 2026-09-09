import { describe, expect, test } from 'vitest'

import { ApiError } from '../../api/transport'
import { FALLBACK_ADVICE } from './reportDraftMessages'
import { RECONCILE_TRANSITION, savedReportsRefusalMessage } from './savedReportsMessages'
import { promotionRefusalMessage } from './useReportPromotion'

/**
 * The two refusal tables are closed: the workspace names a code, and only a sentence
 * this repository authored may reach the reader.
 *
 * `assertOk` copies `error.code` out of the response envelope with no allowlist, so a
 * code that happens to name an `Object.prototype` member used to resolve through the
 * prototype chain: `constructor` handed back a function, `__proto__` handed back the
 * prototype itself. Concatenated into an alert, that put `function Object() { [native
 * code] }` or `[object Object]` on screen instead of the authored fallback.
 *
 * Every ApiError below is real and synthetic. No network, no live authority.
 */

const PROMOTION_FALLBACK = 'This report could not be saved to the workspace, so nothing was saved.'
const SAVED_FALLBACK = 'This saved report could not be updated, so nothing was written.'

/** The five `Object.prototype` names, plus a code no table has ever heard of. */
const UNKNOWN_CODES = [
  'constructor',
  'toString',
  '__proto__',
  'hasOwnProperty',
  'valueOf',
  'report_code_no_table_holds',
]

/** Nothing a lookup could have produced by accident may survive as reader copy. */
function expectAuthoredProse(message: unknown): asserts message is string {
  expect(typeof message).toBe('string')
  const text = message as string
  expect(text).not.toContain('native code')
  expect(text).not.toContain('[object')
  expect(text).not.toContain('function ')
  expect(text).not.toContain('refused by the workspace prose')
}

describe('a workspace code never reaches the reader as anything but authored copy', () => {
  test.each(UNKNOWN_CODES)('promotion refusal for %s is the authored fallback', (code) => {
    // The code arrives exactly as the transport would hand it over.
    const error = new ApiError(409, code, 'refused by the workspace prose')
    const message = promotionRefusalMessage(error.code)
    expectAuthoredProse(message)
    expect(message).toBe(PROMOTION_FALLBACK + ' ' + FALLBACK_ADVICE)
  })

  test.each(UNKNOWN_CODES)('saved-report refusal for %s is the authored fallback', (code) => {
    const error = new ApiError(409, code, 'refused by the workspace prose')
    const message = savedReportsRefusalMessage(error.code)
    expectAuthoredProse(message)
    expect(message).toBe(SAVED_FALLBACK)
  })
})

describe('codes the tables do hold still return their own sentence', () => {
  test('a known promotion refusal keeps its authored copy and the fallback advice', () => {
    const error = new ApiError(409, 'report_document_limit', 'limit')
    const message = promotionRefusalMessage(error.code)
    expectAuthoredProse(message)
    expect(message).toContain('reached the most saved reports it can hold')
    expect(message).toContain('Archived reports still count toward that limit')
    expect(message.endsWith(FALLBACK_ADVICE)).toBe(true)
  })

  test('a known saved-report refusal keeps its authored copy, shared consts included', () => {
    expect(savedReportsRefusalMessage('report_revision_limit')).toBe(
      'This report cannot take another authored version, so nothing was written.'
      + ' Finalize, archive, and restore still use the last confirmed text.',
    )
    // A table entry that reuses an exported sentence still comes back as that sentence.
    expect(savedReportsRefusalMessage('report_revision_conflict')).toBe(RECONCILE_TRANSITION)
  })
})
