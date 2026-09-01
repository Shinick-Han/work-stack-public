const CHANNEL_NAME = 'workstack:planning-change:v1'
export const CROSS_TAB_STORAGE_KEY = 'workstack:planning-change:v1'

interface PlanningChangeSignal {
  at: number
  nonce: string
  source: string
  version: 1
}

const sourceId = (() => {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `${Date.now()}:${Math.random().toString(36).slice(2)}`
})()
let publisherChannel: BroadcastChannel | null = null

function isSignal(value: unknown): value is PlanningChangeSignal {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<PlanningChangeSignal>
  return candidate.version === 1
    && typeof candidate.source === 'string'
    && typeof candidate.nonce === 'string'
    && typeof candidate.at === 'number'
}

function decodeSignal(value: unknown): PlanningChangeSignal | null {
  if (isSignal(value)) return value
  if (typeof value !== 'string') return null
  try {
    const parsed: unknown = JSON.parse(value)
    return isSignal(parsed) ? parsed : null
  } catch {
    return null
  }
}

/**
 * Publishes only a content-free refresh hint. No Task fields, Capture fields, reply
 * material, or docking payload bytes cross the browser-tab boundary.
 */
export function publishPlanningChange(): void {
  if (typeof window === 'undefined') return
  const signal: PlanningChangeSignal = {
    at: Date.now(),
    nonce: typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2),
    source: sourceId,
    version: 1,
  }

  try {
    if ('BroadcastChannel' in window) {
      publisherChannel ??= new BroadcastChannel(CHANNEL_NAME)
      publisherChannel.postMessage(signal)
    }
  } catch {
    // The storage signal below remains the compatibility path.
  }

  try {
    window.localStorage.setItem(CROSS_TAB_STORAGE_KEY, JSON.stringify(signal))
  } catch {
    // Cross-tab refresh is best-effort; server revision checks remain authoritative.
  }
}

export function subscribePlanningChanges(onChange: () => void): () => void {
  if (typeof window === 'undefined') return () => undefined
  const seenNonces = new Set<string>()
  const receive = (value: unknown) => {
    const signal = decodeSignal(value)
    if (!signal || signal.source === sourceId || seenNonces.has(signal.nonce)) return
    seenNonces.add(signal.nonce)
    if (seenNonces.size > 64) seenNonces.delete(seenNonces.values().next().value!)
    onChange()
  }
  const onStorage = (event: StorageEvent) => {
    if (event.key === CROSS_TAB_STORAGE_KEY && event.newValue) receive(event.newValue)
  }
  window.addEventListener('storage', onStorage)

  let channel: BroadcastChannel | null = null
  try {
    if ('BroadcastChannel' in window) {
      channel = new BroadcastChannel(CHANNEL_NAME)
      channel.addEventListener('message', (event) => receive(event.data))
    }
  } catch {
    channel = null
  }

  return () => {
    window.removeEventListener('storage', onStorage)
    channel?.close()
  }
}
