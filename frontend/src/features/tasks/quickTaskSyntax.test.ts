import { describe, expect, test } from 'vitest'
import { parseQuickTaskSyntax, QuickTaskSyntaxError } from './quickTaskSyntax'

describe('explicit Quick Add shorthand', () => {
  test('extracts only documented tokens and preserves the remaining title', () => {
    expect(parseQuickTaskSyntax(
      'Prepare leadership highlights !P1 @O-4 /plan 2026-09-02 /due 2026-09-04 #report #leadership /estimate 90m',
      ['O-4'],
    )).toEqual({
      title: 'Prepare leadership highlights',
      priority: 'P1',
      objectiveId: 'O-4',
      scheduled: '2026-09-02',
      due: '2026-09-04',
      tags: ['report', 'leadership'],
      estimateMinutes: 90,
    })
  })

  test('accepts hours and normalizes objective and priority casing', () => {
    expect(parseQuickTaskSyntax('Review signals !p2 @o-4 /estimate 2h', ['O-4'])).toMatchObject({
      title: 'Review signals', priority: 'P2', objectiveId: 'O-4', estimateMinutes: 120,
    })
  })

  test.each([
    ['Duplicate fields', 'Do it !P1 !P2'],
    ['Unknown objective', 'Do it @O-9'],
    ['Invalid date', 'Do it /plan 2026-9-2'],
    ['Unknown command', 'Do it /later tomorrow'],
    ['Excessive estimate', 'Do it /estimate 25h'],
    ['Missing title', '!P1 #report'],
  ])('%s refuses without returning a partial parse', (_label, value) => {
    expect(() => parseQuickTaskSyntax(value, ['O-4'])).toThrow(QuickTaskSyntaxError)
  })
})
