import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, createIdempotencyKey } from '../../api/client'
import { Button, ErrorState, LoadingBlock, Pill } from '../../components/Primitives'
import type { KeyResult, Objective, ObjectiveDetail, WorkspaceProjection } from '../../domain/types'
import { getErrorMessage, getObjectiveTitle } from '../../utils/format'
import {
  blockingDependenciesFromIndex,
  indexDependencyTasks,
} from '../tasks/taskRelationships'

interface ObjectiveHubPageProps {
  objectiveId: string
  onCreateAlignedTask: (objectiveId: string) => void
  onNotice: (message: string, tone?: 'success' | 'error') => void
  onOpenTask: (taskId: string) => void
  onSelectObjective: (objectiveId: string) => void
  workspace: WorkspaceProjection
}

const objectiveStatuses = ['active', 'done', 'dropped'] as const

function currentQuarter() {
  const now = new Date()
  return `${now.getFullYear()}-Q${Math.floor(now.getMonth() / 3) + 1}`
}

function averageProgress(objective: Objective) {
  const results = objective.key_results ?? []
  if (!results.length) return 0
  return Math.round(results.reduce((sum, result) => sum + (result.progress ?? 0), 0) / results.length)
}

function KeyResultEditor({
  disabled,
  keyResult,
  onSave,
}: {
  disabled: boolean
  keyResult: KeyResult
  onSave: (text: string, target: string, progress: number, status: string) => void
}) {
  const [text, setText] = useState(keyResult.text)
  const [target, setTarget] = useState(keyResult.target ?? '')
  const [progress, setProgress] = useState(keyResult.progress ?? 0)
  const [status, setStatus] = useState(keyResult.status ?? 'active')

  useEffect(() => {
    setText(keyResult.text)
    setTarget(keyResult.target ?? '')
    setProgress(keyResult.progress ?? 0)
    setStatus(keyResult.status ?? 'active')
  }, [keyResult.progress, keyResult.status, keyResult.target, keyResult.text])

  const dirty = text !== keyResult.text
    || target !== (keyResult.target ?? '')
    || progress !== (keyResult.progress ?? 0)
    || status !== (keyResult.status ?? 'active')
  return (
    <article className="kr-row">
      <label><span>Key Result description</span><input disabled={disabled} onChange={(event) => setText(event.target.value)} value={text} /></label>
      <label><span>Key Result target</span><input disabled={disabled} onChange={(event) => setTarget(event.target.value)} placeholder="No target label" value={target} /></label>
      <label><span>Progress</span><div className="kr-progress-input"><input disabled={disabled} max="100" min="0" onChange={(event) => setProgress(Number(event.target.value))} type="range" value={progress} /><output>{progress}%</output></div></label>
      <label><span>Status</span><select disabled={disabled} onChange={(event) => setStatus(event.target.value)} value={status}>{objectiveStatuses.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
      <Button disabled={disabled || !dirty || !text.trim()} onClick={() => onSave(text.trim(), target.trim(), progress, status)} variant="secondary">Save KR</Button>
    </article>
  )
}

function ObjectiveEditor({
  disabled,
  objective,
  onSave,
}: {
  disabled: boolean
  objective: Objective
  onSave: (title: string, quarter: string) => void
}) {
  const [title, setTitle] = useState(getObjectiveTitle(objective))
  const [quarter, setQuarter] = useState(objective.quarter ?? '')

  useEffect(() => {
    setTitle(getObjectiveTitle(objective))
    setQuarter(objective.quarter ?? '')
  }, [objective.objective, objective.quarter, objective.title])

  const dirty = title !== getObjectiveTitle(objective) || quarter !== (objective.quarter ?? '')
  return (
    <div className="objective-editor">
      <label><span>Objective title</span><input disabled={disabled} onChange={(event) => setTitle(event.target.value)} value={title} /></label>
      <label><span>Objective quarter</span><input disabled={disabled} onChange={(event) => setQuarter(event.target.value)} placeholder="2026-Q4" value={quarter} /></label>
      <Button disabled={disabled || !dirty || !title.trim() || !quarter.trim()} onClick={() => onSave(title.trim(), quarter.trim())} variant="secondary">Save Objective</Button>
    </div>
  )
}

export function ObjectiveHubPage({ objectiveId, onCreateAlignedTask, onNotice, onOpenTask, onSelectObjective, workspace }: ObjectiveHubPageProps) {
  const queryClient = useQueryClient()
  const sortedObjectives = useMemo(() => [...workspace.objectives].sort((left, right) => (
    (left.status === 'active' ? 0 : 1) - (right.status === 'active' ? 0 : 1)
    || (left.quarter ?? '').localeCompare(right.quarter ?? '')
    || left.id.localeCompare(right.id)
  )), [workspace.objectives])
  const selectedId = sortedObjectives.some((objective) => objective.id === objectiveId)
    ? objectiveId
    : sortedObjectives[0]?.id ?? ''
  const [krText, setKrText] = useState('')
  const [krTarget, setKrTarget] = useState('')
  const krIntentKey = useRef<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [newObjective, setNewObjective] = useState('')
  const [newQuarter, setNewQuarter] = useState(currentQuarter)
  const objectiveIntentKey = useRef<string | null>(null)

  useEffect(() => {
    if (selectedId && selectedId !== objectiveId) onSelectObjective(selectedId)
  }, [objectiveId, onSelectObjective, selectedId])

  const detailQuery = useQuery({
    enabled: Boolean(selectedId),
    queryKey: ['objective', selectedId],
    queryFn: () => api.getObjective(selectedId),
  })

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['workspace'] }),
      queryClient.invalidateQueries({ queryKey: ['objective', selectedId] }),
    ])
  }

  const addKeyResult = useMutation({
    mutationFn: ({ detail, key }: { detail: ObjectiveDetail; key: string }) => (
      api.addKeyResult(selectedId, krText, krTarget, detail.objective.revision, key)
    ),
    onSuccess: async () => {
      krIntentKey.current = null
      setKrText('')
      setKrTarget('')
      await refresh()
      onNotice('Key Result added')
    },
    onError: (error) => onNotice(getErrorMessage(error), 'error'),
  })

  const createObjective = useMutation({
    mutationFn: ({ key }: { key: string }) => api.createObjective(newObjective.trim(), newQuarter.trim(), key),
    onSuccess: async (created) => {
      objectiveIntentKey.current = null
      setCreateOpen(false)
      setNewObjective('')
      await queryClient.invalidateQueries({ queryKey: ['workspace'] })
      onSelectObjective(created.id)
      onNotice(`Objective ${created.id} added`)
    },
    onError: (error) => onNotice(getErrorMessage(error), 'error'),
  })

  const objectiveUpdate = useMutation({
    mutationFn: ({ fields, revision }: { fields: { objective?: string; quarter?: string; status?: string }; revision: number }) => api.patchObjective(selectedId, fields, revision),
    onSuccess: async () => { await refresh(); onNotice('Objective status updated') },
    onError: async (error) => { await refresh(); onNotice(getErrorMessage(error), 'error') },
  })

  const objectiveContentUpdate = useMutation({
    mutationFn: ({ objective, quarter, revision }: { objective: string; quarter: string; revision: number }) => api.patchObjective(selectedId, { objective, quarter }, revision),
    onSuccess: async () => { await refresh(); onNotice('Objective updated') },
    onError: async (error) => { await refresh(); onNotice(getErrorMessage(error), 'error') },
  })

  const keyResultUpdate = useMutation({
    mutationFn: ({ keyResultId, fields, revision }: { keyResultId: string; fields: { text: string; target: string; progress: number; status: string }; revision: number }) => (
      api.patchKeyResult(selectedId, keyResultId, fields, revision)
    ),
    onSuccess: async () => { await refresh(); onNotice('Key Result updated') },
    onError: async (error) => { await refresh(); onNotice(getErrorMessage(error), 'error') },
  })

  const submitKeyResult = (event: FormEvent) => {
    event.preventDefault()
    if (!detailQuery.data || !krText.trim()) return
    krIntentKey.current ??= createIdempotencyKey()
    addKeyResult.mutate({ detail: detailQuery.data, key: krIntentKey.current })
  }

  const resetKrIntent = (setter: (value: string) => void, value: string) => {
    krIntentKey.current = null
    setter(value)
  }

  const submitObjective = (event: FormEvent) => {
    event.preventDefault()
    if (!newObjective.trim() || !newQuarter.trim()) return
    objectiveIntentKey.current ??= createIdempotencyKey()
    createObjective.mutate({ key: objectiveIntentKey.current })
  }

  const detail = detailQuery.data
  const dependencyIndex = useMemo(
    () => indexDependencyTasks(workspace.tasks),
    [workspace.tasks],
  )
  const linkedTaskReadiness = useMemo(() => (detail?.tasks ?? []).map((task) => ({
    task,
    blockers: blockingDependenciesFromIndex(dependencyIndex, task),
  })), [dependencyIndex, detail?.tasks])
  const readinessCounts = linkedTaskReadiness.reduce((counts, { task, blockers }) => {
    if (task.status === 'done') counts.done += 1
    else if (task.status === 'dropped') counts.dropped += 1
    else if (blockers.length) counts.blocked += 1
    else counts.actionable += 1
    return counts
  }, { actionable: 0, blocked: 0, done: 0, dropped: 0 })
  return (
    <section className="objective-hub" aria-labelledby="objective-hub-heading">
      <header className="page-heading">
        <div><div className="eyebrow"><span className="live-dot" /> Objective hub</div><h1 id="objective-hub-heading">Make the goal–work chain explicit.</h1><p>Define measurable outcomes, update them deliberately, and inspect the Tasks carrying each Objective.</p></div>
        <div className="page-heading__actions"><Button onClick={() => { setCreateOpen(!createOpen); setNewObjective(''); setNewQuarter(currentQuarter()); objectiveIntentKey.current = null; createObjective.reset() }} variant="primary">{createOpen ? 'Cancel new Objective' : 'New Objective'}</Button></div>
      </header>
      {createOpen ? (
        <form className="objective-create-panel" onSubmit={submitObjective}>
          <label><span>New Objective title</span><input disabled={createObjective.isPending} onChange={(event) => { objectiveIntentKey.current = null; setNewObjective(event.target.value) }} placeholder="What outcome should work align to?" value={newObjective} /></label>
          <label><span>New Objective quarter</span><input disabled={createObjective.isPending} onChange={(event) => { objectiveIntentKey.current = null; setNewQuarter(event.target.value) }} pattern="[0-9]{4}-Q[1-4]" value={newQuarter} /></label>
          <Button disabled={createObjective.isPending || !newObjective.trim() || !newQuarter.trim()} type="submit" variant="secondary">{createObjective.isError ? 'Retry unchanged Objective' : createObjective.isPending ? 'Creating…' : 'Create Objective'}</Button>
        </form>
      ) : null}
      {!sortedObjectives.length ? <div className="objective-empty"><h2>No Objectives yet</h2><p>Use New Objective above to create the first outcome, then add measurable Key Results here.</p></div> : (
        <div className="objective-hub-layout">
          <aside className="objective-index" aria-label="Objectives">
            {sortedObjectives.map((objective) => {
              const linked = workspace.tasks.filter((task) => task.objective_ids.includes(objective.id)).length
              return <button aria-current={objective.id === selectedId ? 'true' : undefined} className={objective.id === selectedId ? 'is-active' : ''} key={objective.id} onClick={() => onSelectObjective(objective.id)} type="button"><span><strong>{objective.id}</strong><Pill tone={objective.status === 'active' ? 'accent' : 'neutral'}>{objective.status ?? 'active'}</Pill></span><b>{getObjectiveTitle(objective)}</b><small>{objective.quarter ?? 'No quarter'} · {linked} Tasks · {averageProgress(objective)}%</small></button>
            })}
          </aside>
          <div className="objective-detail-stage">
            {detailQuery.isPending ? <LoadingBlock label="Opening Objective…" /> : detailQuery.isError || !detail ? <ErrorState message={getErrorMessage(detailQuery.error)} onRetry={() => void detailQuery.refetch()} /> : (
              <>
                <section className="objective-overview">
                  <div className="objective-overview__identity"><span>{detail.objective.id} · {detail.objective.quarter ?? 'No quarter'}</span><h2>{getObjectiveTitle(detail.objective)}</h2><small>Revision {detail.objective.revision} · updated {detail.objective.updated_at ?? 'unknown'}</small></div>
                  <ObjectiveEditor disabled={objectiveContentUpdate.isPending || objectiveUpdate.isPending} objective={detail.objective} onSave={(objective, quarter) => objectiveContentUpdate.mutate({ objective, quarter, revision: detail.objective.revision })} />
                  <label><span>Objective status</span><select disabled={objectiveUpdate.isPending || objectiveContentUpdate.isPending} onChange={(event) => objectiveUpdate.mutate({ fields: { status: event.target.value }, revision: detail.objective.revision })} value={detail.objective.status ?? 'active'}>{objectiveStatuses.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
                </section>
                <section className="objective-metrics" aria-label="Objective metrics"><div><span>Average KR</span><strong>{averageProgress(detail.objective)}%</strong></div><div><span>Key Results</span><strong>{detail.objective.key_results?.length ?? 0}</strong></div><div><span>Linked Tasks</span><strong>{detail.tasks.length}</strong></div><div><span>Recorded changes</span><strong>{detail.activity.length}</strong></div></section>
                <section className="kr-panel">
                  <header><div><span>Measurable outcomes</span><h3>Key Results</h3></div></header>
                  {detail.objective.key_results?.length ? <div className="kr-list">{detail.objective.key_results.map((keyResult) => <KeyResultEditor disabled={keyResultUpdate.isPending} key={keyResult.id} keyResult={keyResult} onSave={(text, target, progress, status) => keyResultUpdate.mutate({ keyResultId: keyResult.id, fields: { text, target, progress, status }, revision: detail.objective.revision })} />)}</div> : <p className="objective-inline-empty">No Key Results yet. Add the first measurable outcome below.</p>}
                  <form className="kr-create" onSubmit={submitKeyResult}><label><span>New Key Result</span><input disabled={addKeyResult.isPending} onChange={(event) => resetKrIntent(setKrText, event.target.value)} placeholder="A measurable outcome" value={krText} /></label><label><span>Target label</span><input disabled={addKeyResult.isPending} onChange={(event) => resetKrIntent(setKrTarget, event.target.value)} placeholder="e.g. 5 days or 95%" value={krTarget} /></label><Button disabled={addKeyResult.isPending || !krText.trim()} type="submit" variant="primary">{addKeyResult.isError ? 'Retry unchanged KR' : addKeyResult.isPending ? 'Adding…' : 'Add Key Result'}</Button></form>
                </section>
                <section className="objective-task-panel"><header><div><span>Planning alignment</span><h3>Linked Tasks</h3></div><Button icon="plus" onClick={() => onCreateAlignedTask(detail.objective.id)} variant="secondary">Create aligned task</Button></header><div aria-label="Objective execution readiness" className="objective-readiness"><div><span>Actionable</span><strong>{readinessCounts.actionable}</strong></div><div><span>Blocked</span><strong>{readinessCounts.blocked}</strong></div><div><span>Done</span><strong>{readinessCounts.done}</strong></div><div><span>Dropped</span><strong>{readinessCounts.dropped}</strong></div></div>{linkedTaskReadiness.length ? <div className="objective-task-list">{linkedTaskReadiness.map(({ task, blockers }) => { const blocked = (task.status === 'open' || task.status === 'started') && blockers.length > 0; return <button key={task.id} onClick={() => onOpenTask(task.id)} type="button"><span><strong>{task.id}</strong><Pill tone={task.status === 'done' ? 'success' : blocked ? 'warning' : 'neutral'}>{blocked ? 'blocked' : task.status}</Pill></span><b>{task.title}</b><small>{task.priority} · revision {task.revision}</small>{blocked ? <small className="objective-task-blocker">Waiting on {blockers.map((blocker) => blocker.id).join(', ')}</small> : null}</button> })}</div> : <p className="objective-inline-empty">No Tasks are aligned to this Objective yet.</p>}</section>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
