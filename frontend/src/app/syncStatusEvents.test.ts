import { vi } from 'vitest'
import { subscribeSyncStatusEvents } from './syncStatusEvents'

class FakeEventSource {
  static instance: FakeEventSource | null = null
  listeners = new Map<string, EventListener>()
  close = vi.fn()

  constructor(public url: string, public options?: EventSourceInit) {
    FakeEventSource.instance = this
  }

  addEventListener(name: string, listener: EventListener) {
    this.listeners.set(name, listener)
  }

  emit(name: string, data: string) {
    this.listeners.get(name)?.({ data } as MessageEvent)
  }
}

test('delivers a bounded SSE sync hint and closes the transport', () => {
  vi.stubGlobal('EventSource', FakeEventSource)
  const onChange = vi.fn()
  const unsubscribe = subscribeSyncStatusEvents(onChange)

  expect(FakeEventSource.instance?.url).toBe('/api/v1/events')
  FakeEventSource.instance?.emit('sync', JSON.stringify({ generation: 13, state: 'external-change-detected' }))
  expect(onChange).toHaveBeenCalledWith({ generation: 13, state: 'external-change-detected' })

  unsubscribe()
  expect(FakeEventSource.instance?.close).toHaveBeenCalledOnce()
})

test('falls back to event-driven focus refresh when SSE construction fails', () => {
  vi.stubGlobal('EventSource', class { constructor() { throw new Error('unavailable') } })
  const onChange = vi.fn()
  const unsubscribe = subscribeSyncStatusEvents(onChange)

  window.dispatchEvent(new Event('focus'))
  expect(onChange).toHaveBeenCalledOnce()
  unsubscribe()
  window.dispatchEvent(new Event('focus'))
  expect(onChange).toHaveBeenCalledOnce()
})
