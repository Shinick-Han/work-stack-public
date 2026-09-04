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

  emit(name: string, data: string, lastEventId = '') {
    this.listeners.get(name)?.({ data, lastEventId } as MessageEvent)
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

const checkpoint = {
  event_id: 3, kind: 'agent.checkpoint.committed', workspace_uid: '123e4567-e89b-42d3-a456-426614174000',
  task_id: 'T-0001', date: '2026-09-03', checkpoint_id: `CP-${'a'.repeat(64)}`,
  done_count: 1, next_count: 0, blocker_count: 0, first_for_task: true, origin: 'agent-cli-v1', replayed: false,
}

test('the same existing source dispatches only strict typed changes to the optional checkpoint receiver', () => {
  vi.stubGlobal('EventSource', FakeEventSource)
  const onChange = vi.fn()
  const onCheckpoint = vi.fn()
  const unsubscribe = subscribeSyncStatusEvents(onChange, onCheckpoint)
  const source = FakeEventSource.instance!
  source.emit('workstack.change.v1', JSON.stringify(checkpoint), '3')
  expect(onCheckpoint).toHaveBeenCalledExactlyOnceWith(checkpoint)
  expect(onChange).not.toHaveBeenCalled()
  source.emit('sync', JSON.stringify({ generation: 3, state: 'in-sync' }))
  expect(onChange).toHaveBeenCalledExactlyOnceWith({ generation: 3, state: 'in-sync' })
  expect(FakeEventSource.instance).toBe(source)
  unsubscribe()
})

test('malformed typed frames and mismatched cursors cannot invoke either callback', () => {
  vi.stubGlobal('EventSource', FakeEventSource)
  const change = vi.fn()
  const checkpointReceived = vi.fn()
  const unsubscribe = subscribeSyncStatusEvents(change, checkpointReceived)
  const source = FakeEventSource.instance!
  for (const value of ['{', JSON.stringify({ ...checkpoint, title: 'CANARY' }), JSON.stringify({ ...checkpoint, replayed: true })]) {
    source.emit('workstack.change.v1', value, '3')
  }
  source.emit('workstack.change.v1', JSON.stringify(checkpoint), '2')
  expect(change).not.toHaveBeenCalled()
  expect(checkpointReceived).not.toHaveBeenCalled()
  unsubscribe()
})

test('queued old-source listeners are inert after cleanup and focus/online listeners are removed', () => {
  vi.stubGlobal('EventSource', FakeEventSource)
  const change = vi.fn()
  const received = vi.fn()
  const unsubscribe = subscribeSyncStatusEvents(change, received)
  const old = FakeEventSource.instance!
  window.dispatchEvent(new Event('online'))
  expect(change).toHaveBeenCalledTimes(1)
  unsubscribe()
  old.emit('workstack.change.v1', JSON.stringify(checkpoint), '3')
  old.emit('sync', JSON.stringify({ generation: 3 }))
  window.dispatchEvent(new Event('focus'))
  window.dispatchEvent(new Event('online'))
  expect(change).toHaveBeenCalledTimes(1)
  expect(received).not.toHaveBeenCalled()
  expect(old.close).toHaveBeenCalledTimes(1)
})
