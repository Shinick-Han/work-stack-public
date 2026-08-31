import { useMemo, useState } from 'react'
import { Button, Pill } from '../../components/Primitives'
import type { Capture } from '../../domain/types'
import { copyTextToClipboard } from '../../utils/clipboard'
import { buildLeadershipCollectionBrief, buildLeadershipSignals, LEADERSHIP_LEVELS, type LeadershipLevel } from './leadershipSignals'

interface LeadershipSignalsPanelProps {
  captures: Capture[]
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onOpenCapture: (captureId: string) => void
}

const levelLabels: Record<LeadershipLevel, string> = {
  manager: 'Manager',
  'manager-manager': "Manager's manager",
  vp: 'VP',
}

function currentIsoWeek() {
  const date = new Date()
  const day = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()))
  day.setUTCDate(day.getUTCDate() + 4 - (day.getUTCDay() || 7))
  const yearStart = new Date(Date.UTC(day.getUTCFullYear(), 0, 1))
  const week = Math.ceil((((day.valueOf() - yearStart.valueOf()) / 86400000) + 1) / 7)
  return `${day.getUTCFullYear()}-W${String(week).padStart(2, '0')}`
}

export function LeadershipSignalsPanel({ captures, onNotice, onOpenCapture }: LeadershipSignalsPanelProps) {
  const available = useMemo(() => buildLeadershipSignals(captures), [captures])
  const [selectedWeek, setSelectedWeek] = useState<string | undefined>()
  const projection = useMemo(() => buildLeadershipSignals(captures, selectedWeek), [captures, selectedWeek])
  const collectionWeek = projection.week ?? currentIsoWeek()

  const copyBrief = async () => {
    try {
      await copyTextToClipboard(buildLeadershipCollectionBrief(collectionWeek.toUpperCase()))
      onNotice('Read-only weekly-report collection request copied')
    } catch {
      onNotice('Clipboard access is unavailable in this browser.', 'error')
    }
  }

  return (
    <section className="leadership-signals" aria-labelledby="leadership-signals-heading">
      <header>
        <div><span>Leadership signals</span><h2 id="leadership-signals-heading">Weekly highlights across reporting levels</h2><p>Only tagged, sanitized Capture packets are compared. No report prose is inferred or retained here.</p></div>
        <div className="leadership-signals__actions">
          {available.availableWeeks.length ? <label><span>Week</span><select onChange={(event) => setSelectedWeek(event.target.value)} value={projection.week ?? ''}>{available.availableWeeks.map((week) => <option key={week} value={week}>{week.toUpperCase()}</option>)}</select></label> : null}
          <Button icon="command" onClick={() => void copyBrief()} variant="secondary">Copy collection request</Button>
        </div>
      </header>

      <div className="leadership-coverage" aria-label="Leadership report coverage">
        {LEADERSHIP_LEVELS.map((level) => <div className={projection.coverage.includes(level) ? 'is-covered' : ''} key={level}><span>{levelLabels[level]}</span><strong>{projection.coverage.includes(level) ? 'Ready' : 'Missing'}</strong></div>)}
      </div>

      {!projection.reports.length ? <p className="review-empty">No sanitized weekly reports are tagged for {collectionWeek.toUpperCase()}. Copy the read-only request, run it through the authenticated OOB agent, then review and import the packets in Context Inbox.</p> : (
        <div className="leadership-signals__grid">
          <section><h3>Cross-level themes</h3>{projection.themes.length ? <div className="leadership-theme-list">{projection.themes.map((theme) => <article key={theme.id}><div><strong>{theme.label}</strong><span>{theme.levels.map((level) => levelLabels[level]).join(' → ')}</span></div><div>{theme.crossLevel ? <Pill tone="warning">Repeated</Pill> : null}{theme.reachesVp ? <Pill tone="accent">VP visible</Pill> : null}</div></article>)}</div> : <p className="review-empty">No explicit theme tags in this week’s reports.</p>}</section>
          <section><h3>Source reports</h3><div className="leadership-report-list">{projection.reports.map((report) => <button key={report.capture.id} onClick={() => onOpenCapture(report.capture.id)} type="button"><span><strong>{levelLabels[report.level]}</strong>{report.capture.source.display_title}</span><small>{report.capture.normalized.summary}</small></button>)}</div></section>
          <section><h3>Explicit actions</h3>{projection.actions.length ? <div className="leadership-action-list">{projection.actions.map((action, index) => <button key={`${action.captureId}-${action.id ?? index}`} onClick={() => onOpenCapture(action.captureId)} type="button"><strong>{action.title}</strong><span>{levelLabels[action.level]} · {action.priority}{action.due ? ` · due ${action.due}` : ''}</span></button>)}</div> : <p className="review-empty">No explicit action items were supplied. Work Stack will not invent them.</p>}</section>
        </div>
      )}
      {projection.invalidCount ? <p className="leadership-signals__warning" role="status">{projection.invalidCount} weekly-report capture{projection.invalidCount === 1 ? '' : 's'} need exactly one week and one leadership level tag before analysis.</p> : null}
    </section>
  )
}
