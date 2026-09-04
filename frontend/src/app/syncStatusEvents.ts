import {
  decodeCheckpointTransition,
  decodeWorkspaceChange,
  WORKSPACE_CHANGE_EVENT,
  type CheckpointCommittedEvent,
  type CheckpointTransitionEvent,
} from './workspaceChangeEvents'

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
export function subscribeSyncStatusEvents(
  onChange: (signal?: SyncStatusSignal) => void,
  onCheckpoint?: (event: CheckpointCommittedEvent) => void,
  onTransition?: (event: CheckpointTransitionEvent) => void,
): () => void {
  if (typeof window === 'undefined') return () => undefined

  let active = true
  const onFallback = () => { if (active) onChange() }
  window.addEventListener('focus', onFallback)
  window.addEventListener('online', onFallback)

  let source: EventSource | null = null
  if (typeof EventSource !== 'undefined') {
    try {
      source = new EventSource('/api/v1/events', { withCredentials: true })
      const receive = (event: MessageEvent<string>) => {
        const signal = decodeSignal(event.data)
        if (active && signal) onChange(signal)
      }
      source.addEventListener('message', receive as EventListener)
      source.addEventListener('generation', receive as EventListener)
      source.addEventListener('sync', receive as EventListener)
      source.addEventListener(WORKSPACE_CHANGE_EVENT, ((event: MessageEvent<string>) => {
        // Each variant is decoded by its own strict schema. A committed
        // notice never satisfies the transition branch and vice versa.
        const checkpoint = decodeWorkspaceChange(event.data, event.lastEventId)
        if (active && checkpoint) {
          onCheckpoint?.(checkpoint)
          return
        }
        const transition = decodeCheckpointTransition(event.data, event.lastEventId)
        if (active && transition) onTransition?.(transition)
      }) as EventListener)
    } catch {
      // Manual review/refresh plus focus and online events remain available.
      source = null
    }
  }

  return () => {
    active = false
    window.removeEventListener('focus', onFallback)
    window.removeEventListener('online', onFallback)
    source?.close()
  }
}
