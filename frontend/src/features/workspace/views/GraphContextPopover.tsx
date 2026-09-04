import { useEffect, useId, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { microsoftProviderGates } from '../../../config/providerGates'
import { TaskContextTimeline } from '../../../components/TaskContextTimeline'
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
  prerequisites,
}: GraphContextPopoverProps) {
  const titleId = useId()
  const dialogRef = useRef<HTMLDialogElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const detail = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => api.getTask(taskId),
    retry: false,
  })
  const restoreRef = useRef<{
    trigger: HTMLButtonElement | null
    fallbackSelector?: string
    onFocusReturned?: () => void
    ownerAliveRef?: { current: boolean }
  }>({ trigger, fallbackSelector: focusFallbackSelector, onFocusReturned, ownerAliveRef })
  restoreRef.current = { trigger, fallbackSelector: focusFallbackSelector, onFocusReturned, ownerAliveRef }

  // Cancels a restoration watch installed by a PREVIOUS effect setup. React
  // StrictMode replays setup/cleanup in development, and that replay focuses the
  // close control below; without this the replayed blur would fire the previous
  // watch and release the pin of a popup that is still open.
  const cancelPreviousWatchRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    // Effect-lifetime cancellation, deliberately BEFORE the close control takes
    // focus. This is not a real final close, so it never notifies the owner.
    cancelPreviousWatchRef.current?.()
    cancelPreviousWatchRef.current = null

    const dialog = dialogRef.current
    dialog?.showModal()
    closeRef.current?.focus()
    return () => {
      dialog?.close()
      const {
        trigger: original,
        fallbackSelector,
        onFocusReturned: notify,
        ownerAliveRef: alive,
      } = restoreRef.current
      const ownerGone = () => alive ? !alive.current : false

      if (original?.isConnected) {
        original.focus()
        // Ownership stays with the restored trigger: the caller's pin is
        // released only once focus ACTUALLY leaves it, or once the trigger is
        // removed from the document while it still owns focus.
        let settled = false
        const stop = () => {
          if (settled) return
          settled = true
          original.removeEventListener('blur', release)
          observer.disconnect()
        }
        const release = () => {
          if (settled) return
          // A dead owner may neither move focus nor clear a later owner's state.
          const abandoned = ownerGone()
          stop()
          if (!abandoned) notify?.()
        }
        const observer = new MutationObserver(() => {
          if (settled) return
          if (ownerGone()) { stop(); return }
          if (!original.isConnected) {
            focusFallback(fallbackSelector)
            release()
          }
        })
        original.addEventListener('blur', release, { once: true })
        observer.observe(document.body, { childList: true, subtree: true })
        // A later setup of THIS effect owns the right to cancel this watch.
        cancelPreviousWatchRef.current = stop
        return
      }

      // No connected trigger to return to: hand focus to a stable surviving
      // Workspace control rather than dropping it on the document body.
      focusFallback(fallbackSelector)
      notify?.()
    }
    // The dialog is keyed by task, so this runs once per open popup.
  }, [])

  const wrongTask = detail.data !== undefined && detail.data.task.id !== taskId
  return createPortal(<dialog
    ref={dialogRef}
    className="wsv-graph-context"
    aria-labelledby={titleId}
    onCancel={(event) => { event.preventDefault(); onClose() }}
    onPointerDown={(event) => event.stopPropagation()}
    onClick={(event) => event.stopPropagation()}
    onWheel={(event) => event.stopPropagation()}
    onKeyDown={(event) => {
      event.stopPropagation()
      if (event.key === 'Escape') { event.preventDefault(); onClose() }
      if (event.key === 'Tab') {
        const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), a[href]'))
        const first = controls[0]
        const last = controls.at(-1)
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }}
  >
    <header className="wsv-graph-context__header">
      <div><p>{taskId} · Context</p><h2 id={titleId} title={taskTitle}>{taskTitle}</h2></div>
      <button ref={closeRef} type="button" onClick={onClose} aria-label="Close context">Close</button>
    </header>
    <div className="wsv-graph-context__body" aria-busy={detail.isFetching}>
      {detail.isPending ? <p role="status">Loading context…</p> : detail.isError || wrongTask ? (
        <div role="alert"><p>Context could not be loaded for this task.</p>
          <button type="button" disabled={detail.isFetching} onClick={() => { void detail.refetch() }}>Retry</button>
        </div>
      ) : <TaskContextTimeline context={detail.data.context} providerGates={microsoftProviderGates} />}
    </div>
    {prerequisites ? <div className="wsv-graph-context__prereq">{prerequisites}</div> : null}
    <footer className="wsv-graph-context__footer"><button type="button" onClick={onOpenTask}>Open task</button></footer>
  </dialog>, document.body)
}

/** Moves focus to a stable surviving control, never leaving it on BODY. */
function focusFallback(selector?: string) {
  if (!selector) return
  const fallback = document.querySelector<HTMLElement>(selector)
  if (!fallback) return
  if (!fallback.hasAttribute('tabindex')) fallback.setAttribute('tabindex', '-1')
  fallback.focus()
}
