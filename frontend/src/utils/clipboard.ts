export async function copyTextToClipboard(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value)
      return
    } catch {
      // Some browser permission policies expose Clipboard but reject the call. The
      // short-lived fallback keeps the explicit user click useful on local installs.
    }
  }

  const field = document.createElement('textarea')
  field.value = value
  field.setAttribute('readonly', '')
  field.style.position = 'fixed'
  field.style.opacity = '0'
  document.body.appendChild(field)
  field.select()
  const copied = typeof document.execCommand === 'function' && document.execCommand('copy')
  field.remove()
  if (!copied) throw new Error('Clipboard access is unavailable in this browser.')
}
