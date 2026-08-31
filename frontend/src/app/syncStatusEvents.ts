export interface SyncStatusSignal {
  generation?: number
  state?: string
}

function decodeSignal(value: string): SyncStatusSignal | null {
  try {
    const candidate: unknown = JSON.parse(value)
    if (!candidate || typeof candidate !== 'object') return null
    const signal = candidate as SyncStatusSignal
    if (signal.generation !== undefined && (!Number.isInteger(signal.generation) || signal.generation < 0)) return null
    if (signal.state !== undefined && typeof signal.state !== 'string') return null
    return signal.generation !== undefined || signal.state !== undefined ? signal : null
  } catch {
    return null
  }
}

/**
 * SSE is the primary remote-change hint. Focus/online events are deliberately
 * event-driven compatibility fallbacks; this client never introduces a polling
 * loop. Every hint triggers an authoritative HTTP refetch in the caller.
 */
export function subscribeSyncStatusEvents(onChange: (signal?: SyncStatusSignal) => void): () => void {
  if (typeof window === 'undefined') return () => undefined

  const onFallback = () => onChange()
  window.addEventListener('focus', onFallback)
  window.addEventListener('online', onFallback)

  let source: EventSource | null = null
  if (typeof EventSource !== 'undefined') {
    try {
      source = new EventSource('/api/v1/events', { withCredentials: true })
      const receive = (event: MessageEvent<string>) => {
        const signal = decodeSignal(event.data)
        if (signal) onChange(signal)
      }
      source.addEventListener('message', receive as EventListener)
      source.addEventListener('generation', receive as EventListener)
      source.addEventListener('sync', receive as EventListener)
    } catch {
      // Manual review/refresh plus focus and online events remain available.
      source = null
    }
  }

  return () => {
    window.removeEventListener('focus', onFallback)
    window.removeEventListener('online', onFallback)
    source?.close()
  }
}
