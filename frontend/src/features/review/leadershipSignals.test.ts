import { expect, test } from 'vitest'
import { capture } from '../../test/fixtures'
import { buildLeadershipCollectionBrief, buildLeadershipSignals } from './leadershipSignals'

function report(id: string, level: string, themes: string[]) {
  return {
    ...capture,
    id,
    source: { ...capture.source, display_title: `${level} weekly` },
    normalized: {
      ...capture.normalized,
      tags: ['weekly-report', 'week:2026-W35', `level:${level}`, ...themes.map((theme) => `theme:${theme}`)],
    },
  }
}

test('finds cross-level and VP-visible themes without inferring from report prose', () => {
  const projection = buildLeadershipSignals([
    report('C-0001', 'manager', ['release-risk', 'staffing']),
    report('C-0002', 'manager-manager', ['release-risk']),
    report('C-0003', 'vp', ['release-risk', 'customer-trust']),
  ])
  expect(projection.week).toBe('2026-w35')
  expect(projection.coverage).toEqual(['manager', 'manager-manager', 'vp'])
  expect(projection.themes[0]).toMatchObject({
    id: 'release-risk', crossLevel: true, reachesVp: true,
    levels: ['manager', 'manager-manager', 'vp'],
  })
  expect(projection.actions).toHaveLength(3)
})

test('rejects ambiguous metadata and collection brief forbids remote writes and raw retention', () => {
  const ambiguous = report('C-0004', 'vp', [])
  ambiguous.normalized.tags.push('level:manager')
  expect(buildLeadershipSignals([ambiguous]).invalidCount).toBe(1)
  const brief = buildLeadershipCollectionBrief('2026-W35')
  expect(brief).toContain('Read only')
  expect(brief).toContain('raw_retained=false')
  expect(brief).toContain('Do not create or change Work Stack Tasks')
})
