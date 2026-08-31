import type { Capture, CaptureAction } from '../../domain/types'

export const LEADERSHIP_LEVELS = ['manager', 'manager-manager', 'vp'] as const
export type LeadershipLevel = (typeof LEADERSHIP_LEVELS)[number]

export interface LeadershipReport {
  capture: Capture
  level: LeadershipLevel
  week: string
  themes: string[]
  signals: string[]
}

export interface LeadershipTheme {
  id: string
  label: string
  levels: LeadershipLevel[]
  captureIds: string[]
  crossLevel: boolean
  reachesVp: boolean
}

export interface LeadershipAction extends CaptureAction {
  captureId: string
  level: LeadershipLevel
}

export interface LeadershipSignalsProjection {
  availableWeeks: string[]
  week: string | null
  reports: LeadershipReport[]
  coverage: LeadershipLevel[]
  themes: LeadershipTheme[]
  actions: LeadershipAction[]
  invalidCount: number
}

function taggedValues(tags: string[], prefix: string): string[] {
  return [...new Set(tags.filter((tag) => tag.startsWith(prefix)).map((tag) => tag.slice(prefix.length)).filter(Boolean))]
}

export function leadershipReports(captures: Capture[]) {
  const reports: LeadershipReport[] = []
  let invalidCount = 0
  for (const capture of captures) {
    const tags = capture.normalized.tags.map((tag) => tag.toLocaleLowerCase())
    if (!tags.includes('weekly-report')) continue
    const weeks = taggedValues(tags, 'week:')
    const levels = taggedValues(tags, 'level:').filter((level): level is LeadershipLevel => (
      LEADERSHIP_LEVELS.includes(level as LeadershipLevel)
    ))
    if (weeks.length !== 1 || levels.length !== 1) {
      invalidCount += 1
      continue
    }
    reports.push({
      capture,
      level: levels[0],
      week: weeks[0],
      themes: taggedValues(tags, 'theme:'),
      signals: taggedValues(tags, 'signal:'),
    })
  }
  return { reports, invalidCount }
}

export function buildLeadershipSignals(captures: Capture[], selectedWeek?: string): LeadershipSignalsProjection {
  const parsed = leadershipReports(captures)
  const availableWeeks = [...new Set(parsed.reports.map((report) => report.week))].sort().reverse()
  const week = selectedWeek && availableWeeks.includes(selectedWeek) ? selectedWeek : availableWeeks[0] ?? null
  const reports = parsed.reports.filter((report) => report.week === week).sort((left, right) => (
    LEADERSHIP_LEVELS.indexOf(left.level) - LEADERSHIP_LEVELS.indexOf(right.level)
    || left.capture.id.localeCompare(right.capture.id)
  ))
  const coverage = LEADERSHIP_LEVELS.filter((level) => reports.some((report) => report.level === level))
  const themeMap = new Map<string, { levels: Set<LeadershipLevel>; captures: Set<string> }>()
  for (const report of reports) {
    for (const theme of report.themes) {
      const entry = themeMap.get(theme) ?? { levels: new Set<LeadershipLevel>(), captures: new Set<string>() }
      entry.levels.add(report.level)
      entry.captures.add(report.capture.id)
      themeMap.set(theme, entry)
    }
  }
  const themes = [...themeMap.entries()].map(([id, entry]) => {
    const levels = LEADERSHIP_LEVELS.filter((level) => entry.levels.has(level))
    return {
      id,
      label: id.replace(/[-_]+/g, ' '),
      levels,
      captureIds: [...entry.captures].sort(),
      crossLevel: levels.length >= 2,
      reachesVp: levels.includes('vp'),
    }
  }).sort((left, right) => (
    Number(right.crossLevel) - Number(left.crossLevel)
    || Number(right.reachesVp) - Number(left.reachesVp)
    || right.levels.length - left.levels.length
    || left.label.localeCompare(right.label)
  ))
  const actions = reports.flatMap((report) => report.capture.normalized.action_items.map((action) => ({
    ...action,
    captureId: report.capture.id,
    level: report.level,
  })))
  return { availableWeeks, week, reports, coverage, themes, actions, invalidCount: parsed.invalidCount }
}

export function buildLeadershipCollectionBrief(week: string): string {
  return [
    'WORK STACK — READ-ONLY LEADERSHIP WEEKLY SIGNAL COLLECTION',
    `Target week: ${week}`,
    '',
    'Using the already authenticated Microsoft 365 OOB skills, locate the weekly report for each of these three roles: my manager, my manager\'s manager, and the VP. Read only; do not reply, edit, move, or create remote content.',
    '',
    'Return one sanitized Work Stack Capture Packet v1 per report. Never include raw mail/chat/document bodies, recipients, attachments, credentials, headers, or tokens. Set raw_retained=false and summarize only the minimum needed for planning.',
    '',
    'Required normalized tags on every packet:',
    '- weekly-report',
    `- week:${week}`,
    '- exactly one of level:manager, level:manager-manager, level:vp',
    '- one or more theme:<stable-kebab-case-theme> tags when themes exist',
    '- signal:risk, signal:decision, signal:priority, or signal:ask when applicable',
    '',
    'Put explicit requested actions in normalized.action_items. Do not create or change Work Stack Tasks. Return the packets for human review and import through Context Inbox.',
  ].join('\n')
}
