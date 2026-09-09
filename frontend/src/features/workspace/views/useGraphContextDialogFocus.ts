import { useEffect, useRef } from 'react'

/** What the popup owes focus back to once it closes. */
export type GraphContextFocusReturn = {
  /** May be absent or already detached; restoration then uses the fallback. */
  trigger: HTMLButtonElement | null
  /** A stable surviving Workspace element focus returns to when the trigger is gone. */
  fallbackSelector?: string
  /** Fired once focus restoration has been handed off, so the owner may release its pin. */
  onFocusReturned?: () => void
  /**
   * Liveness of the owning Graph/Workspace. Deferred restoration work checks it,
   * so a watch can never act after its owner is gone.
   */
  ownerAliveRef?: { current: boolean }
}

/**
 * Owns the modal's focus lifetime: it shows the dialog, takes focus on the close
 * control, and on teardown returns focus to the opener — waiting, when the opener
 * survives, until focus actually leaves it before releasing the caller's pin.
 */
export function useGraphContextDialogFocus(target: GraphContextFocusReturn) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const restoreRef = useRef<GraphContextFocusReturn>(target)
  restoreRef.current = target

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

  return { dialogRef, closeRef }
}

/** Moves focus to a stable surviving control, never leaving it on BODY. */
function focusFallback(selector?: string) {
  if (!selector) return
  const fallback = document.querySelector<HTMLElement>(selector)
  if (!fallback) return
  if (!fallback.hasAttribute('tabindex')) fallback.setAttribute('tabindex', '-1')
  fallback.focus()
}
