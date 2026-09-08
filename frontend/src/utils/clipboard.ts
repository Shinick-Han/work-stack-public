export const CLIPBOARD_CANCELLED_MESSAGE = 'The clipboard copy is no longer current and was cancelled.'

function cancelled() {
  return new Error(CLIPBOARD_CANCELLED_MESSAGE)
}

export function isClipboardCancelled(error: unknown) {
  return error instanceof Error && error.message === CLIPBOARD_CANCELLED_MESSAGE
}

/**
 * Copies text, falling back to a short-lived textarea when the async Clipboard API
 * is present but rejected by a permission policy.
 *
 * `isCurrent` lets a caller that owns a switchable payload cancel the copy at the
 * points where this helper is still able to stop: before the native write starts,
 * after a native rejection but before any fallback side effect, and immediately
 * before `document.execCommand('copy')`. A native write that was already submitted
 * cannot be reversed, so a copy that resolves is never undone here — only the
 * fallback path and any not-yet-started write are cancellable. Callers that pass no
 * guard keep the previous unconditional behaviour.
 */
export async function copyTextToClipboard(
  value: string,
  isCurrent: () => boolean = () => true,
): Promise<void> {
  const requireCurrent = () => {
    if (!isCurrent()) throw cancelled()
  }

  requireCurrent()
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value)
      return
    } catch {
      // Some browser permission policies expose Clipboard but reject the call. The
      // short-lived fallback keeps the explicit user click useful on local installs.
    }
    // The rejection may have resolved long after the click; refuse before touching
    // the document on behalf of a payload the caller no longer owns.
    requireCurrent()
  }

  const field = document.createElement('textarea')
  field.value = value
  field.setAttribute('readonly', '')
  field.style.position = 'fixed'
  field.style.opacity = '0'
  document.body.appendChild(field)
  field.select()
  let copied = false
  try {
    requireCurrent()
    copied = typeof document.execCommand === 'function' && document.execCommand('copy')
  } finally {
    field.remove()
  }
  if (!copied) throw new Error('Clipboard access is unavailable in this browser.')
}
