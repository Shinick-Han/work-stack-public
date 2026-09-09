import { useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  MUTATION_NOTICE_DEFAULT_LIMIT,
  describeMutationNoticeListFailure,
  listMutationNotices,
  type MutationUndoTarget,
} from '../../api/mutationNotices'
import { Button, EmptyState, ErrorState, LoadingBlock, Pill } from '../../components/Primitives'
import type { Task } from '../../domain/types'
import {
  MUTATION_NOTICE_COPY,
  indexTasksByUid,
  projectMutationNotices,
  revisionSummary,
  statusLabel,
  type MutationNoticeRow,
} from './MutationNoticePanelModel'
import { useMutationNoticeUndo, type MutationUndoModel } from './MutationNoticePanelUndo'
import './MutationNoticePanel.css'

/**
 * `Recent changes`: the reader-facing surface of the shipped mutation-notice contract.
 *
 * The scope is bounded on purpose. One page of the owner's own newest-first list, an
 * explicit refresh, and explicit paging older — never a background poll, never an
 * accumulating feed, and never a scroll that quietly asks for more. Every row shows who
 * made the change, from where, to which Task, and from which status to which; a row the
 * owner does not admit as reversible shows no Undo control at all.
 */
export interface MutationNoticePanelProps {
  workspaceId: string
  /** This workspace's own projection: the only thing that may name a Task on this surface. */
  tasks: readonly Task[]
  onOpenTask: (taskId: string) => void
}

function StatusTransition({ row }: { row: MutationNoticeRow }) {
  if (row.statusBefore === null || row.statusAfter === null) {
    return <Pill tone="neutral">{MUTATION_NOTICE_COPY.readOnly}</Pill>
  }
  // The summary above already reads "Task status open to started", so the pills repeat
  // it visually and are hidden from assistive technology rather than read twice.
  return (
    <span aria-hidden="true" className="mutation-notices__transition">
      <Pill tone={row.statusBefore}>{statusLabel(row.statusBefore)}</Pill>
      <span className="mutation-notices__arrow">→</span>
      <Pill tone={row.statusAfter}>{statusLabel(row.statusAfter)}</Pill>
    </span>
  )
}

function TaskLink({
  onOpenTask,
  row,
}: {
  onOpenTask: (taskId: string) => void
  row: MutationNoticeRow
}) {
  const taskId = row.taskId
  if (taskId === null) {
    // Two different facts, and only one of them is about a Task. A Task notice whose
    // entity this projection does not carry is genuinely a Task out of view; every other
    // kind is named for what it is, because no Task was ever involved in it.
    const label = row.isTaskEntity ? MUTATION_NOTICE_COPY.unknownTask : row.entityLabel
    return <span className="mutation-notices__task is-unknown">{label}</span>
  }
  return (
    <button
      className="mutation-notices__task"
      onClick={() => onOpenTask(taskId)}
      title={`${MUTATION_NOTICE_COPY.openTask} ${taskId}`}
      type="button"
    >
      <strong>{taskId}</strong>
      <span>{row.taskTitle}</span>
    </button>
  )
}

function undoLabel(phase: string | undefined): string {
  if (phase === 'pending') return MUTATION_NOTICE_COPY.undoing
  if (phase === 'unknown') return MUTATION_NOTICE_COPY.undoRetry
  return MUTATION_NOTICE_COPY.undo
}

/**
 * The Undo control and this row's outcome. The control exists only for a target the owner
 * admitted, and it is disabled — never hidden — while any attempt is unresolved, so a
 * reader can still see which row the panel is holding.
 */
function UndoCell({
  onUndo,
  row,
  undo,
}: {
  onUndo: (target: MutationUndoTarget, taskId: string | null) => void
  row: MutationNoticeRow
  undo: MutationUndoModel
}) {
  const state = undo.state?.noticeId === row.noticeId ? undo.state : null
  const settled = state !== null && (state.phase === 'undone' || state.phase === 'conflict' || state.phase === 'failed')
  return (
    <div className="mutation-notices__act">
      {row.undo ? (
        <Button
          // A settled undo is spent: the row's recorded revision has already been
          // compensated, so offering it again could only earn a conflict.
          disabled={state?.phase === 'undone' || (undo.locked && state?.phase !== 'unknown')}
          onClick={() => onUndo(row.undo as MutationUndoTarget, row.taskId)}
          variant="secondary"
        >
          {undoLabel(state?.phase)}
        </Button>
      ) : null}
      {state?.message ? (
        <p
          aria-label={MUTATION_NOTICE_COPY.outcome}
          className="mutation-notices__outcome"
          data-phase={state.phase}
          role="status"
        >
          {state.message}
        </p>
      ) : null}
      {settled ? (
        <Button onClick={undo.onDismiss} variant="ghost">{MUTATION_NOTICE_COPY.dismiss}</Button>
      ) : null}
    </div>
  )
}

function NoticeRow({
  onOpenTask,
  row,
  undo,
}: {
  onOpenTask: (taskId: string) => void
  row: MutationNoticeRow
  undo: MutationUndoModel
}) {
  return (
    <li className="mutation-notices__row">
      <div className="mutation-notices__what">
        <strong>{row.summary}</strong>
        <StatusTransition row={row} />
      </div>
      <div className="mutation-notices__who">
        <TaskLink onOpenTask={onOpenTask} row={row} />
        <small>{row.sourceLabel} · {row.actor} · {revisionSummary(row)}</small>
      </div>
      <UndoCell onUndo={undo.onUndo} row={row} undo={undo} />
    </li>
  )
}

interface PageState {
  workspaceId: string
  cursor: string | null
  depth: number
}

/**
 * One page of the owner's list, plus where the reader stands in it.
 *
 * Cursors are minted for one workspace and the owner refuses one from another, so the
 * page resets during the render that observes a new workspace rather than a commit later.
 * `depth` is only how far back the reader has walked: there is no cursor history, and
 * `Back to newest` returns to the unparameterised first page instead of inventing one.
 */
function useNoticePage(workspaceId: string) {
  const [page, setPage] = useState<PageState>({ workspaceId, cursor: null, depth: 0 })
  const settled = useRef(page)
  if (page.workspaceId !== workspaceId) {
    const reset = { workspaceId, cursor: null, depth: 0 }
    settled.current = reset
    setPage(reset)
  }
  const current = page.workspaceId === workspaceId ? page : settled.current
  return {
    cursor: current.cursor,
    depth: current.depth,
    older: (cursor: string) => setPage({ workspaceId, cursor, depth: current.depth + 1 }),
    newest: () => setPage({ workspaceId, cursor: null, depth: 0 }),
  }
}

function PageControls({
  disabled,
  depth,
  nextCursor,
  onNewest,
  onOlder,
}: {
  disabled: boolean
  depth: number
  nextCursor: string | null
  onNewest: () => void
  onOlder: (cursor: string) => void
}) {
  return (
    <div className="mutation-notices__paging">
      {nextCursor === null ? (
        <small>{MUTATION_NOTICE_COPY.oldest}</small>
      ) : (
        <Button disabled={disabled} onClick={() => onOlder(nextCursor)}>{MUTATION_NOTICE_COPY.older}</Button>
      )}
      {depth > 0 ? (
        <Button disabled={disabled} onClick={onNewest} variant="ghost">{MUTATION_NOTICE_COPY.newest}</Button>
      ) : null}
    </div>
  )
}

export function MutationNoticePanel({ onOpenTask, tasks, workspaceId }: MutationNoticePanelProps) {
  const page = useNoticePage(workspaceId)
  const undo = useMutationNoticeUndo(workspaceId)
  const notices = useQuery({
    queryKey: ['mutation-notices', workspaceId, page.cursor],
    queryFn: () => listMutationNotices({ cursor: page.cursor, limit: MUTATION_NOTICE_DEFAULT_LIMIT }),
  })
  const tasksByUid = useMemo(() => indexTasksByUid(tasks), [tasks])
  const rows = useMemo(
    () => projectMutationNotices(notices.data?.items ?? [], tasksByUid),
    [notices.data, tasksByUid],
  )

  return (
    <section aria-labelledby="mutation-notices-heading" className="mutation-notices">
      <header className="mutation-notices__header">
        <div>
          <h2 id="mutation-notices-heading">{MUTATION_NOTICE_COPY.heading}</h2>
          <p>{MUTATION_NOTICE_COPY.lead}</p>
        </div>
        <Button
          disabled={undo.locked || notices.isFetching}
          icon="refresh"
          onClick={() => void notices.refetch()}
        >
          {notices.isFetching ? MUTATION_NOTICE_COPY.refreshing : MUTATION_NOTICE_COPY.refresh}
        </Button>
      </header>
      {notices.isPending ? <LoadingBlock label={MUTATION_NOTICE_COPY.loading} /> : null}
      {notices.isError ? (
        <ErrorState
          message={describeMutationNoticeListFailure(notices.error)}
          onRetry={() => void notices.refetch()}
        />
      ) : null}
      {!notices.isPending && !notices.isError && rows.length === 0 ? (
        <EmptyState icon="activity" title={MUTATION_NOTICE_COPY.emptyTitle}>
          <p>{MUTATION_NOTICE_COPY.emptyBody}</p>
        </EmptyState>
      ) : null}
      {rows.length ? (
        <ul className="mutation-notices__list">
          {rows.map((row) => (
            <NoticeRow key={row.noticeId} onOpenTask={onOpenTask} row={row} undo={undo} />
          ))}
        </ul>
      ) : null}
      {notices.data ? (
        <PageControls
          depth={page.depth}
          disabled={undo.locked || notices.isFetching}
          nextCursor={notices.data.next_cursor}
          onNewest={page.newest}
          onOlder={page.older}
        />
      ) : null}
    </section>
  )
}
