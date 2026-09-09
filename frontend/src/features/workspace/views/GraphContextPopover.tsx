import { useId, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { microsoftProviderGates } from '../../../config/providerGates'
import { TaskContextTimeline } from '../../../components/TaskContextTimeline'
import { GraphContextCardComposer, type GraphContextCardComposerHandle } from './GraphContextCardComposer'
import { graphContextDialogFocusables } from './GraphContextCardComposerModel'
import { useGraphContextDialogFocus } from './useGraphContextDialogFocus'
import './GraphContextPopover.css'

interface GraphContextPopoverProps {
  taskId: string
  taskTitle: string
  /** May be absent or already detached; restoration then uses the fallback. */
  trigger: HTMLButtonElement | null
  /** A stable surviving Workspace element focus returns to when the trigger is gone. */
  focusFallbackSelector?: string
  onClose: () => void
  onOpenTask: () => void
  /** Fired once focus restoration has been handed off, so the owner may release its pin. */
  onFocusReturned?: () => void
  /**
   * Liveness of the owning Graph/Workspace. Deferred restoration work checks it,
   * so a watch can never act after its owner is gone.
   */
  ownerAliveRef?: { current: boolean }
  /** Current workspace identity; composer treats a change as a different owner. */
  workspaceId?: string
  /** Page-owned prerequisite controls; inside the modal so they stay reachable. */
  prerequisites?: ReactNode
}

/** Mounted only for an open task; the existing task query owns response identity. */
export function GraphContextPopover({
  taskId,
  taskTitle,
  trigger,
  focusFallbackSelector,
  onClose,
  onOpenTask,
  onFocusReturned,
  ownerAliveRef,
  workspaceId = '',
  prerequisites,
}: GraphContextPopoverProps) {
  const titleId = useId()
  const composerRef = useRef<GraphContextCardComposerHandle>(null)
  const escapeLockRef = useRef(false)
  const detail = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => api.getTask(taskId),
    retry: false,
  })
  const { dialogRef, closeRef } = useGraphContextDialogFocus({
    trigger,
    fallbackSelector: focusFallbackSelector,
    onFocusReturned,
    ownerAliveRef,
  })

  const wrongTask = detail.data !== undefined && detail.data.task.id !== taskId
  const requestDismiss = (proceed: () => void) => {
    if (!composerRef.current) { proceed(); return }
    composerRef.current.requestDismiss(proceed)
  }
  const requestEscapeClose = () => {
    if (escapeLockRef.current) return
    escapeLockRef.current = true
    queueMicrotask(() => { escapeLockRef.current = false })
    requestDismiss(onClose)
  }
  return createPortal(<dialog
    ref={dialogRef}
    className="wsv-graph-context"
    aria-labelledby={titleId}
    onCancel={(event) => { event.preventDefault(); requestEscapeClose() }}
    onPointerDown={(event) => event.stopPropagation()}
    onClick={(event) => event.stopPropagation()}
    onWheel={(event) => event.stopPropagation()}
    onKeyDown={(event) => {
      event.stopPropagation()
      if (event.key === 'Escape') { event.preventDefault(); requestEscapeClose() }
      if (event.key === 'Tab') {
        const trap = event.currentTarget.querySelector<HTMLElement>('[role="alertdialog"]') ?? event.currentTarget
        const controls = graphContextDialogFocusables(trap)
        const first = controls[0]
        const last = controls.at(-1)
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }}
  >
    <header className="wsv-graph-context__header">
      <div><p>{taskId} · Context</p><h2 id={titleId} title={taskTitle}>{taskTitle}</h2></div>
      <button ref={closeRef} type="button" onClick={() => requestDismiss(onClose)} aria-label="Close context">Close</button>
    </header>
    <div className="wsv-graph-context__body" aria-busy={detail.isFetching}>
      {detail.isPending ? <p role="status">Loading context…</p> : detail.isError || wrongTask ? (
        <div role="alert"><p>Context could not be loaded for this task.</p>
          <button type="button" disabled={detail.isFetching} onClick={() => { void detail.refetch() }}>Retry</button>
        </div>
      ) : <TaskContextTimeline context={detail.data.context} providerGates={microsoftProviderGates} />}
      <GraphContextCardComposer
        key={`${workspaceId}:${taskId}`}
        ref={composerRef}
        taskId={taskId}
        workspaceId={workspaceId}
        ownerAliveRef={ownerAliveRef}
      />
    </div>
    {prerequisites ? <div className="wsv-graph-context__prereq">{prerequisites}</div> : null}
    <footer className="wsv-graph-context__footer"><button type="button" onClick={() => requestDismiss(onOpenTask)}>Open task</button></footer>
  </dialog>, document.body)
}
