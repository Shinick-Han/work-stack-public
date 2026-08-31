import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Button } from '../../components/Primitives'
import type { WorkSession, WorkSessionEntryInput, WorkSessionProjection } from '../../domain/types'

function formatElapsed(totalSeconds: number) {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':')
}

function useLiveElapsed(session: WorkSession | null) {
  const [clock, setClock] = useState(() => Date.now())
  useEffect(() => {
    if (session?.state !== 'running') return undefined
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [session?.id, session?.state])
  return useMemo(() => {
    if (!session || session.state !== 'running') return session?.elapsed_seconds ?? 0
    const anchor = Date.parse(session.updated_at)
    const additional = Number.isFinite(anchor) ? Math.max(0, Math.floor((clock - anchor) / 1000)) : 0
    return session.elapsed_seconds + additional
  }, [clock, session])
}

function splitLines(value: string) {
  return value.split('\n').map((item) => item.trim()).filter(Boolean)
}

function PendingWorklog({
  disabled,
  onRecord,
  session,
}: {
  disabled: boolean
  onRecord: (sessionId: string, input: WorkSessionEntryInput) => Promise<void>
  session: WorkSession
}) {
  const [done, setDone] = useState('')
  const [next, setNext] = useState('')
  const [blockers, setBlockers] = useState('')
  const input = { done: splitLines(done), next: splitLines(next), blockers: splitLines(blockers) }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!input.done.length && !input.next.length && !input.blockers.length) return
    await onRecord(session.id, input)
  }
  return (
    <form className="work-session-card work-session-card--pending" onSubmit={(event) => void submit(event)}>
      <div className="work-session-card__heading">
        <div>
          <span>Worklog ready</span>
          <strong>{session.task}</strong>
        </div>
        <time>{formatElapsed(session.elapsed_seconds)}</time>
      </div>
      <p>The session is stopped. Review these notes before they become part of your Worklog.</p>
      <div className="work-session-form">
        <label>Done<textarea aria-label="Done" onChange={(event) => setDone(event.target.value)} placeholder="One item per line" value={done} /></label>
        <label>Next<textarea aria-label="Next" onChange={(event) => setNext(event.target.value)} placeholder="One item per line" value={next} /></label>
        <label>Blockers<textarea aria-label="Blockers" onChange={(event) => setBlockers(event.target.value)} placeholder="One item per line" value={blockers} /></label>
      </div>
      <Button disabled={disabled || (!input.done.length && !input.next.length && !input.blockers.length)} type="submit" variant="primary">Add to worklog</Button>
    </form>
  )
}

export function WorkSessionPanel({
  disabled,
  onRecord,
  onTransition,
  projection,
}: {
  disabled: boolean
  onRecord: (sessionId: string, input: WorkSessionEntryInput) => Promise<void>
  onTransition: (sessionId: string, action: 'pause' | 'resume' | 'stop') => Promise<void>
  projection: WorkSessionProjection
}) {
  const elapsed = useLiveElapsed(projection.current)
  return (
    <div className="work-session-stack" aria-label="Work sessions">
      {projection.current ? (
        <section className="work-session-card work-session-card--active">
          <div className="work-session-card__heading">
            <div>
              <span>Current work session</span>
              <strong>{projection.current.task}</strong>
            </div>
            <time aria-label="Session elapsed time">{formatElapsed(elapsed)}</time>
          </div>
          <p>Human execution only. Task status and revision stay unchanged.</p>
          <div className="work-session-card__actions">
            <Button
              disabled={disabled}
              onClick={() => void onTransition(projection.current!.id, projection.current!.state === 'running' ? 'pause' : 'resume')}
              variant="secondary"
            >{projection.current.state === 'running' ? 'Pause session' : 'Resume session'}</Button>
            <Button disabled={disabled} onClick={() => void onTransition(projection.current!.id, 'stop')} variant="danger">Stop session</Button>
          </div>
        </section>
      ) : null}
      {projection.pending.map((session) => (
        <PendingWorklog disabled={disabled} key={session.id} onRecord={onRecord} session={session} />
      ))}
    </div>
  )
}
