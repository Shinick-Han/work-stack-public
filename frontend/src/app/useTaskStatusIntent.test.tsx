import { act, render } from '@testing-library/react'
import { StrictMode, useLayoutEffect } from 'react'
import { describe, expect, it, vi } from 'vitest'

import {
  useTaskStatusIntent,
  type TaskStatusIntent,
  type TaskStatusIntentOwner,
  type TaskStatusIntentResponse,
  type UseTaskStatusIntentOptions,
  type UseTaskStatusIntentResult,
} from './useTaskStatusIntent'
import type { Task, TaskStatus } from '../domain/types'

/**
 * These tests drive the ACTUAL hook with injected callbacks. There is no
 * test-side copy of its state machine: every expectation is a callback count,
 * an exact frozen key/body, or the hook's own returned state.
 *
 * They cannot establish notice eligibility in the real component, HTTP
 * correctness or server durability; those belong to the integration packet.
 */

const OWNER: TaskStatusIntentOwner = {
  workspaceUid: 'WS-1',
  ownerEpoch: 'epoch-A',
  taskId: 'T-1',
  taskUid: 'uid-1',
}

function makeTask(patch: Partial<Task> = {}): Task {
  return {
    id: 'T-1',
    uid: 'uid-1',
    title: 'Write the intent hook',
    detail: '',
    status: 'open',
    priority: 'P2',
    due: null,
    tags: [],
    objective_ids: [],
    parent_id: null,
    dependencies: [],
    subtasks: [],
    notes: [],
    revision: 7,
    context_count: 0,
    ...patch,
  }
}

function receipt(task: Task, replayed = false, status = 200): TaskStatusIntentResponse {
  return { status, body: { data: task, meta: { replayed } } }
}

/** A deferred one-attempt API so a test can observe the pending window. */
function deferred() {
  let settle!: (response: TaskStatusIntentResponse) => void
  let fail!: (error: Error) => void
  const promise = new Promise<TaskStatusIntentResponse>((resolve, reject) => {
    settle = resolve
    fail = reject
  })
  return { promise, settle, fail }
}

interface Harness {
  result: () => UseTaskStatusIntentResult
  rerender: (patch: Partial<UseTaskStatusIntentOptions>) => void
  unmount: () => void
  renders: () => number
}

function renderHook(
  overrides: Partial<UseTaskStatusIntentOptions> = {},
  { strict = false }: { strict?: boolean } = {},
): Harness {
  let latest: UseTaskStatusIntentResult | null = null
  let renders = 0
  let options: UseTaskStatusIntentOptions = {
    owner: OWNER,
    currentTask: makeTask(),
    mutateOnce: vi.fn(async (_intent: TaskStatusIntent) => receipt(makeTask({ status: 'started', revision: 8 }))),
    newKey: vi.fn((kind: string) => `key-${kind}-1`),
    reconcile: vi.fn(),
    isCurrent: vi.fn(() => true),
    ...overrides,
  }

  function Probe(props: { options: UseTaskStatusIntentOptions }) {
    renders += 1
    latest = useTaskStatusIntent(props.options)
    return null
  }

  const tree = (current: UseTaskStatusIntentOptions) =>
    strict ? (
      <StrictMode>
        <Probe options={current} />
      </StrictMode>
    ) : (
      <Probe options={current} />
    )

  const view = render(tree(options))
  return {
    result: () => latest as UseTaskStatusIntentResult,
    rerender: (patch) => {
      options = { ...options, ...patch }
      view.rerender(tree(options))
    },
    unmount: () => view.unmount(),
    renders: () => renders,
  }
}

const flush = async () => {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

/** Child layout effects run before the hook owner's layout effects. */
function renderCommitHook(overrides: Partial<UseTaskStatusIntentOptions> = {}, strict = false) {
  let latest!: UseTaskStatusIntentResult
  let currentEpoch = OWNER.ownerEpoch
  let options: UseTaskStatusIntentOptions = {
    owner: OWNER, currentTask: makeTask(),
    mutateOnce: vi.fn(async () => receipt(makeTask({ status: 'started', revision: 8 }))),
    newKey: vi.fn((kind: string) => `commit-${kind}`), reconcile: vi.fn(),
    isCurrent: (candidate) => candidate.ownerEpoch === currentEpoch,
    ...overrides,
  }
  type OnCommit = (result: UseTaskStatusIntentResult) => void
  function Observer({ result, onCommit }: { result: UseTaskStatusIntentResult; onCommit?: OnCommit }) {
    useLayoutEffect(() => { onCommit?.(result) }, [onCommit])
    return null
  }
  function Probe({ value, onCommit }: { value: UseTaskStatusIntentOptions; onCommit?: OnCommit }) {
    latest = useTaskStatusIntent(value)
    return <Observer result={latest} onCommit={onCommit} />
  }
  const tree = (onCommit?: OnCommit) => strict
    ? <StrictMode><Probe value={options} onCommit={onCommit} /></StrictMode>
    : <Probe value={options} onCommit={onCommit} />
  const view = render(tree())
  return {
    result: () => latest,
    commit: (owner: TaskStatusIntentOwner, onCommit: OnCommit) => {
      currentEpoch = owner.ownerEpoch
      options = { ...options, owner, currentTask: makeTask({ id: owner.taskId, uid: owner.taskUid }) }
      view.rerender(tree(onCommit))
    },
  }
}

describe('UIDS resolver receipt and commit boundaries', () => {
  it('rejects an extra KR reference key and retries the identical frozen intent', async () => {
    const data = makeTask({ status: 'started', revision: 8 })
    const malformed = { ...data, key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-7', extra: true }] }
    const mutateOnce = vi.fn<(intent: TaskStatusIntent) => Promise<TaskStatusIntentResponse>>()
      .mockResolvedValueOnce(receipt(malformed)).mockResolvedValueOnce(receipt(data, true))
    const newKey = vi.fn(() => 'strict-reference-key')
    const harness = renderHook({ mutateOnce, newKey })
    act(() => harness.result().markInProgress())
    await flush()
    expect(harness.result()).toMatchObject({ phase: 'unknown', retryable: true, undoOffer: null })
    act(() => harness.result().retry())
    await flush()
    expect(mutateOnce.mock.calls[1][0]).toBe(mutateOnce.mock.calls[0][0])
    expect(newKey).toHaveBeenCalledTimes(1)
    expect(harness.result().phase).toBe('idle')
  })

  it.each([false, true])('accepts an exact KR reference with replay=%s and opaque Task data', async (replayed) => {
    const data = { ...makeTask({ status: 'started', revision: 8 }),
      key_result_refs: [{ objective_id: 'O-A', key_result_id: 'KR-7' }],
      notes: [{ text: 'opaque note', extra: true }],
      subtasks: [{ id: 'S-A', title: 'opaque subtask', extra: true }], extra: true }
    const harness = renderHook({ mutateOnce: vi.fn(async () => receipt(data, replayed)) })
    act(() => harness.result().markInProgress())
    await flush()
    expect(harness.result()).toMatchObject({ phase: 'idle', retryable: false, undoOffer: { atRevision: 8 } })
  })

  it('publishes fresh B state to the first child commit after unknown A', async () => {
    const harness = renderCommitHook({ mutateOnce: vi.fn(async () => { throw new Error('lost A') }) })
    act(() => harness.result().markInProgress())
    await flush()
    expect(harness.result().phase).toBe('unknown')
    const observe = vi.fn()
    harness.commit({ ...OWNER, ownerEpoch: 'commit-B' }, observe)
    expect(observe.mock.calls[0][0]).toMatchObject({ phase: 'idle', pending: false, retryable: false, canMarkInProgress: true, undoOffer: null })
    expect(harness.result()).toMatchObject({ phase: 'idle', pending: false, retryable: false })
  })

  it.each(['same-owner', 'new-owner'])('retains a child commit action for %s', async (kind) => {
    const call = deferred()
    const mutateOnce = vi.fn((_intent: TaskStatusIntent) => call.promise)
    const harness = renderCommitHook({ mutateOnce })
    const owner = { ...OWNER, ownerEpoch: kind === 'same-owner' ? OWNER.ownerEpoch : 'commit-B' }
    harness.commit(owner, (result) => result.markInProgress())
    expect(mutateOnce).toHaveBeenCalledTimes(1)
    expect(mutateOnce.mock.calls[0][0].owner).toEqual(owner)
    expect(harness.result()).toMatchObject({ phase: 'pending', pending: true })
    await act(async () => { call.settle(receipt(makeTask({ status: 'started', revision: 8 }))) })
    expect(harness.result()).toMatchObject({ phase: 'idle', pending: false, undoOffer: { owner, atRevision: 8 } })
  })

  it.each(['success', 'conflict', 'malformed', 'loss'])('StrictMode B commit retains %s settlement', async (outcome) => {
    const call = deferred()
    const mutateOnce = vi.fn((_intent: TaskStatusIntent) => call.promise)
    const reconcile = vi.fn()
    const newKey = vi.fn(() => 'commit-owned-B')
    const harness = renderCommitHook({ mutateOnce, reconcile, newKey }, true)
    const owner = { ...OWNER, ownerEpoch: 'strict-B' }
    harness.commit(owner, (result) => result.markInProgress())
    expect(harness.result()).toMatchObject({ pending: true, phase: 'pending', retryable: false })
    act(() => harness.result().markInProgress())
    expect(mutateOnce).toHaveBeenCalledTimes(1)
    expect(newKey).toHaveBeenCalledTimes(1)
    await act(async () => {
      if (outcome === 'loss') call.fail(new Error('lost B'))
      else if (outcome === 'conflict') call.settle(receipt(makeTask(), false, 409))
      else if (outcome === 'malformed') call.settle({ status: 200, body: {} } as TaskStatusIntentResponse)
      else call.settle(receipt(makeTask({ status: 'started', revision: 8 })))
    })
    expect(harness.result().pending).toBe(false)
    if (outcome === 'success') {
      expect(harness.result()).toMatchObject({ phase: 'idle', undoOffer: { owner, atRevision: 8 } })
    } else if (outcome === 'conflict') {
      expect(harness.result()).toMatchObject({ phase: 'conflict', retryable: false, undoOffer: null })
      expect(reconcile).toHaveBeenCalledExactlyOnceWith(mutateOnce.mock.calls[0][0])
    } else {
      expect(harness.result()).toMatchObject({ phase: 'unknown', retryable: true, undoOffer: null })
      act(() => harness.result().retry())
      expect(mutateOnce.mock.calls[1][0]).toBe(mutateOnce.mock.calls[0][0])
      expect(newKey).toHaveBeenCalledTimes(1)
      await flush()
    }
  })

  it('refuses an old A retry callback during B child commit', async () => {
    const mutateOnce = vi.fn(async () => { throw new Error('lost A') })
    const harness = renderCommitHook({ mutateOnce }, true)
    act(() => harness.result().markInProgress())
    await flush()
    const retryA = harness.result().retry
    harness.commit({ ...OWNER, ownerEpoch: 'commit-B' }, () => retryA())
    expect(mutateOnce).toHaveBeenCalledTimes(1)
    expect(harness.result()).toMatchObject({ phase: 'idle', retryable: false, pending: false })
  })

  it('a retained same-owner retry reads newly committed callbacks', async () => {
    const first = vi.fn(async (_intent: TaskStatusIntent) => { throw new Error('lost') })
    const newKey = vi.fn(() => 'retained-intent')
    const harness = renderHook({ mutateOnce: first, newKey })
    act(() => harness.result().markInProgress())
    await flush()
    const retry = harness.result().retry
    const current = vi.fn(async (_intent: TaskStatusIntent) => receipt(makeTask({ status: 'started', revision: 8 }), true))
    harness.rerender({ mutateOnce: current })
    act(() => retry())
    await flush()
    expect(first).toHaveBeenCalledTimes(1)
    expect(current).toHaveBeenCalledExactlyOnceWith(first.mock.calls[0][0])
    expect(current.mock.calls[0][0]).toBe(first.mock.calls[0][0])
    expect(newKey).toHaveBeenCalledTimes(1)
    expect(harness.result().phase).toBe('idle')
  })

  it.each(['success', 'conflict', 'malformed', 'loss'])('ignores old A %s while a child-started B is pending', async (outcome) => {
    const oldCall = deferred()
    const newCall = deferred()
    const mutateOnce = vi.fn<(intent: TaskStatusIntent) => Promise<TaskStatusIntentResponse>>()
      .mockReturnValueOnce(oldCall.promise).mockReturnValueOnce(newCall.promise)
    const reconcile = vi.fn()
    const harness = renderCommitHook({ mutateOnce, reconcile }, true)
    act(() => harness.result().markInProgress())
    const owner = { ...OWNER, ownerEpoch: 'new-pending-B' }
    harness.commit(owner, (result) => result.markInProgress())
    await act(async () => {
      if (outcome === 'loss') oldCall.fail(new Error('lost obsolete A'))
      else if (outcome === 'conflict') oldCall.settle(receipt(makeTask(), false, 409))
      else if (outcome === 'malformed') oldCall.settle({ status: 200, body: {} } as TaskStatusIntentResponse)
      else oldCall.settle(receipt(makeTask({ status: 'started', revision: 8 })))
    })
    expect(harness.result()).toMatchObject({ phase: 'pending', pending: true, retryable: false, undoOffer: null })
    expect(reconcile).not.toHaveBeenCalled()
    act(() => harness.result().markInProgress())
    expect(mutateOnce).toHaveBeenCalledTimes(2)
    await act(async () => { newCall.settle(receipt(makeTask({ status: 'started', revision: 8 }))) })
    expect(harness.result()).toMatchObject({ phase: 'idle', pending: false, undoOffer: { owner, atRevision: 8 } })
  })
})

describe('useTaskStatusIntent forward dispatch', () => {
  it('mutates nothing on render, on a rerender or on a new current Task', () => {
    const mutateOnce = vi.fn()
    const newKey = vi.fn()
    const reconcile = vi.fn()
    const harness = renderHook({ mutateOnce, newKey, reconcile })

    harness.rerender({ currentTask: makeTask({ revision: 9 }) })
    harness.rerender({ currentTask: makeTask({ status: 'started', revision: 10 }) })

    expect(mutateOnce).not.toHaveBeenCalled()
    expect(newKey).not.toHaveBeenCalled()
    expect(reconcile).not.toHaveBeenCalled()
    expect(harness.renders()).toBeGreaterThan(1)
  })

  it('sends exactly one frozen intent for one explicit action', async () => {
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) => receipt(makeTask({ status: 'started', revision: 8 })))
    const harness = renderHook({ mutateOnce })

    act(() => harness.result().markInProgress())
    await flush()

    expect(mutateOnce).toHaveBeenCalledTimes(1)
    const intent = mutateOnce.mock.calls[0][0] as TaskStatusIntent
    expect(intent).toMatchObject({
      kind: 'forward',
      taskId: 'T-1',
      taskUid: 'uid-1',
      priorStatus: 'open',
      expectedRevision: 7,
      requestedStatus: 'started',
      key: 'key-forward-1',
      body: { status: 'started', revision: 7 },
    })
    expect(Object.isFrozen(intent)).toBe(true)
    expect(Object.isFrozen(intent.body)).toBe(true)
    expect(Object.isFrozen(intent.owner)).toBe(true)
  })

  it('suppresses a duplicate pending dispatch in the same tick', async () => {
    const pendingCall = deferred()
    const mutateOnce = vi.fn((_intent: TaskStatusIntent) => pendingCall.promise)
    const newKey = vi.fn((kind: string) => `key-${kind}-1`)
    const harness = renderHook({ mutateOnce, newKey })

    act(() => {
      harness.result().markInProgress()
      harness.result().markInProgress()
    })

    expect(mutateOnce).toHaveBeenCalledTimes(1)
    expect(newKey).toHaveBeenCalledTimes(1)
    expect(harness.result().pending).toBe(true)

    await act(async () => {
      pendingCall.settle(receipt(makeTask({ status: 'started', revision: 8 })))
      await Promise.resolve()
    })
    expect(mutateOnce).toHaveBeenCalledTimes(1)
  })

  it('refuses a forward on a Task that is not the authoritative OPEN Task', () => {
    const mutateOnce = vi.fn()
    const started = renderHook({ mutateOnce, currentTask: makeTask({ status: 'started' }) })
    act(() => started.result().markInProgress())
    expect(started.result().canMarkInProgress).toBe(false)

    const replaced = renderHook({ mutateOnce, currentTask: makeTask({ uid: 'uid-2' }) })
    act(() => replaced.result().markInProgress())

    const missing = renderHook({ mutateOnce, currentTask: null })
    act(() => missing.result().markInProgress())

    expect(mutateOnce).not.toHaveBeenCalled()
  })

  it('refuses to dispatch when the synchronous authority has already changed', () => {
    const mutateOnce = vi.fn()
    // The identity change has not rendered yet; isCurrent is the authority.
    const harness = renderHook({ mutateOnce, isCurrent: vi.fn(() => false) })

    act(() => harness.result().markInProgress())

    expect(mutateOnce).not.toHaveBeenCalled()
  })
})

describe('useTaskStatusIntent ambiguity and conflict', () => {
  it('keeps a lost request unknown and retries the SAME body and key', async () => {
    const mutateOnce = vi
      .fn<(intent: TaskStatusIntent) => Promise<TaskStatusIntentResponse>>()
      .mockRejectedValueOnce(new Error('connection lost'))
      .mockResolvedValueOnce(receipt(makeTask({ status: 'started', revision: 8 })))
    const newKey = vi.fn((kind: string) => `key-${kind}-${mutateOnce.mock.calls.length + 1}`)
    const harness = renderHook({ mutateOnce, newKey })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('unknown')
    expect(harness.result().retryable).toBe(true)
    expect(newKey).toHaveBeenCalledTimes(1)

    act(() => harness.result().retry())
    await flush()

    expect(mutateOnce).toHaveBeenCalledTimes(2)
    // No new key was manufactured for the retry.
    expect(newKey).toHaveBeenCalledTimes(1)
    expect(mutateOnce.mock.calls[1][0]).toEqual(mutateOnce.mock.calls[0][0])
    expect(harness.result().phase).toBe('idle')
  })

  it('treats a contradictory success as unknown, not as proof of the write', async () => {
    // 200 with a Task that does not answer the frozen intent.
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) => receipt(makeTask({ status: 'open', revision: 7 })))
    const harness = renderHook({ mutateOnce })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('unknown')
    expect(harness.result().undoOffer).toBeNull()
  })

  it('treats missing metadata as unknown', async () => {
    const malformed = {
      status: 200,
      body: { data: makeTask({ status: 'started', revision: 8 }) },
    } as unknown as TaskStatusIntentResponse
    const harness = renderHook({ mutateOnce: vi.fn(async () => malformed) })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('unknown')
  })

  it('reconciles a determinate 409 for display only, with no second write', async () => {
    const conflict = { status: 409, body: {} } as unknown as TaskStatusIntentResponse
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) => conflict)
    const reconcile = vi.fn()
    const harness = renderHook({ mutateOnce, reconcile })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('conflict')
    expect(reconcile).toHaveBeenCalledTimes(1)
    expect(reconcile.mock.calls[0][0]).toBe(mutateOnce.mock.calls[0][0])
    expect(mutateOnce).toHaveBeenCalledTimes(1)
    expect(harness.result().retryable).toBe(false)
    expect(harness.result().undoOffer).toBeNull()
  })
})

describe('useTaskStatusIntent Undo ownership', () => {
  async function forwardOnce(overrides: Partial<UseTaskStatusIntentOptions> = {}) {
    const newKey = vi.fn((kind: string) => `key-${kind}-1`)
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) => receipt(makeTask({ status: 'started', revision: 8 })))
    const harness = renderHook({ mutateOnce, newKey, ...overrides })
    act(() => harness.result().markInProgress())
    await flush()
    return { harness, mutateOnce, newKey }
  }

  it('offers Undo after a changing forward and restores the captured prior status at the receipt revision', async () => {
    const { harness, mutateOnce, newKey } = await forwardOnce()

    expect(harness.result().undoOffer).toEqual({
      priorStatus: 'open',
      atRevision: 8,
      owner: OWNER,
    })

    act(() => harness.result().undoLast())
    await flush()

    expect(mutateOnce).toHaveBeenCalledTimes(2)
    const undoIntent = mutateOnce.mock.calls[1][0] as TaskStatusIntent
    expect(undoIntent).toMatchObject({
      kind: 'undo',
      requestedStatus: 'open',
      expectedRevision: 8,
      body: { status: 'open', revision: 8 },
    })
    // Distinct immutable key for the Undo.
    expect(undoIntent.key).not.toBe((mutateOnce.mock.calls[0][0] as TaskStatusIntent).key)
    expect(newKey).toHaveBeenCalledTimes(2)
    expect(newKey.mock.calls.map((call) => call[0])).toEqual(['forward', 'undo'])
  })

  it('refuses the forward outright when the Task is already started (eligibility, not a receipt)', async () => {
    // The authoritative Task is already started, so the hook refuses the
    // forward outright: there is no change and therefore nothing to reverse.
    const mutateOnce = vi.fn()
    const harness = renderHook({ mutateOnce, currentTask: makeTask({ status: 'started' }) })

    act(() => harness.result().markInProgress())
    await flush()

    expect(mutateOnce).not.toHaveBeenCalled()
    expect(harness.result().undoOffer).toBeNull()
  })

  it('closes the Undo offer once the Undo itself succeeds', async () => {
    const newKey = vi.fn((kind: string) => `key-${kind}-1`)
    const mutateOnce = vi
      .fn<(intent: TaskStatusIntent) => Promise<TaskStatusIntentResponse>>()
      .mockResolvedValueOnce(receipt(makeTask({ status: 'started', revision: 8 })))
      .mockResolvedValueOnce(receipt(makeTask({ status: 'open', revision: 9 })))
    const harness = renderHook({ mutateOnce, newKey })

    act(() => harness.result().markInProgress())
    await flush()
    act(() => harness.result().undoLast())
    await flush()

    expect(harness.result().undoOffer).toBeNull()
    expect(mutateOnce).toHaveBeenCalledTimes(2)
  })

  it('keeps an older replay as historical evidence and never restores stale Undo ownership', async () => {
    const newKey = vi.fn((kind: string) => `key-${kind}-1`)
    const mutateOnce = vi
      .fn<(intent: TaskStatusIntent) => Promise<TaskStatusIntentResponse>>()
      // forward, then Undo, then an OLD replay of the forward receipt.
      .mockResolvedValueOnce(receipt(makeTask({ status: 'started', revision: 8 })))
      .mockResolvedValueOnce(receipt(makeTask({ status: 'open', revision: 9 })))
      .mockResolvedValueOnce(receipt(makeTask({ status: 'started', revision: 8 }), true))
    const harness = renderHook({ mutateOnce, newKey, currentTask: makeTask() })

    act(() => harness.result().markInProgress())
    await flush()
    expect(harness.result().undoOffer?.atRevision).toBe(8)

    act(() => harness.result().undoLast())
    await flush()
    expect(harness.result().undoOffer).toBeNull()

    // The same forward action replays after the newer Undo is already current.
    harness.rerender({ currentTask: makeTask({ status: 'open', revision: 9 }) })
    act(() => harness.result().markInProgress())
    await flush()

    expect(mutateOnce).toHaveBeenCalledTimes(3)
    // Historical evidence only: no stale Undo ownership, no older state. The
    // stale revision 8 does not answer the request frozen at revision 9, so the
    // hook holds it as UNKNOWN with the same frozen retry instead of settling.
    expect(harness.result().undoOffer).toBeNull()
    expect(harness.result().phase).toBe('unknown')
    expect(harness.result().retryable).toBe(true)
  })

  it('drops the Undo offer when the owner epoch goes A to B to A', async () => {
    const { harness } = await forwardOnce()
    expect(harness.result().undoOffer).not.toBeNull()

    harness.rerender({ owner: { ...OWNER, ownerEpoch: 'epoch-B' } })
    expect(harness.result().undoOffer).toBeNull()

    harness.rerender({ owner: { ...OWNER, ownerEpoch: 'epoch-A' } })
    // The same VALUE returning is a different lifetime; it must not resurrect.
    expect(harness.result().undoOffer).toBeNull()
  })

  it('drops the Undo offer when the Task UID is replaced', async () => {
    const { harness } = await forwardOnce()

    harness.rerender({
      owner: { ...OWNER, taskUid: 'uid-2' },
      currentTask: makeTask({ uid: 'uid-2', revision: 1 }),
    })

    expect(harness.result().undoOffer).toBeNull()
  })

  it('refuses an Undo whose owner is no longer current', async () => {
    const isCurrent = vi.fn(() => true)
    const { harness, mutateOnce } = await forwardOnce({ isCurrent })
    expect(harness.result().undoOffer).not.toBeNull()

    isCurrent.mockReturnValue(false)
    act(() => harness.result().undoLast())
    await flush()

    expect(mutateOnce).toHaveBeenCalledTimes(1)
    expect(harness.result().undoOffer).toBeNull()
  })
})

describe('useTaskStatusIntent lifetime', () => {
  it('ignores a completion that lands after the owner changed', async () => {
    const pendingCall = deferred()
    const isCurrent = vi.fn(() => true)
    const harness = renderHook({ mutateOnce: vi.fn((_intent: TaskStatusIntent) => pendingCall.promise), isCurrent })

    act(() => harness.result().markInProgress())
    isCurrent.mockReturnValue(false)

    await act(async () => {
      pendingCall.settle(receipt(makeTask({ status: 'started', revision: 8 })))
      await Promise.resolve()
    })

    expect(harness.result().undoOffer).toBeNull()
    expect(harness.result().pending).toBe(false)
  })

  it('ignores a completion that lands after unmount', async () => {
    const pendingCall = deferred()
    const reconcile = vi.fn()
    const harness = renderHook({ mutateOnce: vi.fn((_intent: TaskStatusIntent) => pendingCall.promise), reconcile })

    act(() => harness.result().markInProgress())
    harness.unmount()

    await act(async () => {
      pendingCall.settle(receipt(makeTask({ status: 'started', revision: 8 })))
      await Promise.resolve()
    })

    expect(reconcile).not.toHaveBeenCalled()
  })

  it('sends exactly one intent under StrictMode double rendering', async () => {
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) => receipt(makeTask({ status: 'started', revision: 8 })))
    const harness = renderHook({ mutateOnce }, { strict: true })

    act(() => harness.result().markInProgress())
    await flush()

    expect(mutateOnce).toHaveBeenCalledTimes(1)
    expect(harness.result().undoOffer).toEqual({
      priorStatus: 'open',
      atRevision: 8,
      owner: OWNER,
    })
  })

  it('accepts a compatible same-UID revision advance according to the captured intent', async () => {
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) => receipt(makeTask({ status: 'started', revision: 12 })))
    const harness = renderHook({ mutateOnce, currentTask: makeTask({ revision: 11 }) })

    act(() => harness.result().markInProgress())
    await flush()

    const intent = mutateOnce.mock.calls[0][0] as TaskStatusIntent
    expect(intent.expectedRevision).toBe(11)
    expect(intent.body.revision).toBe(11)
    // Undo is expressed at the FORWARD RECEIPT revision, not the captured one.
    expect(harness.result().undoOffer?.atRevision).toBe(12)
  })

  it('exposes a replayed receipt as an ordinary settled success', async () => {
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) =>
      receipt(makeTask({ status: 'started', revision: 8 }), true),
    )
    const harness = renderHook({ mutateOnce })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('idle')
    expect(harness.result().undoOffer?.atRevision).toBe(8)
  })

  it('never writes a status the caller did not request', async () => {
    const seen: TaskStatus[] = []
    const mutateOnce = vi.fn(async (intent: TaskStatusIntent) => {
      seen.push(intent.body.status)
      return receipt(makeTask({ status: intent.requestedStatus, revision: 8 }))
    })
    const harness = renderHook({ mutateOnce })

    act(() => harness.result().markInProgress())
    await flush()
    act(() => harness.result().undoLast())
    await flush()

    expect(seen).toEqual(['started', 'open'])
  })
})


describe('UI1 a successful receipt must answer the frozen request', () => {
  it.each([
    ['a revision far past the captured one', 99],
    ['a fractional revision', 8.5],
    ['a negative revision', -1],
    ['an unsafe integer revision', 9007199254740992],
  ])('keeps %s unknown with the same frozen retry', async (_name, revision) => {
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) =>
      receipt(makeTask({ status: 'started', revision })),
    )
    const harness = renderHook({ mutateOnce })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('unknown')
    expect(harness.result().retryable).toBe(true)
    expect(harness.result().undoOffer).toBeNull()
  })

  it('keeps a four-field stand-in for the Task unknown', async () => {
    const thin = {
      status: 200,
      body: {
        data: { id: 'T-1', uid: 'uid-1', status: 'started', revision: 8 },
        meta: { replayed: false },
      },
    } as unknown as TaskStatusIntentResponse
    const harness = renderHook({ mutateOnce: vi.fn(async (_intent: TaskStatusIntent) => thin) })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('unknown')
    expect(harness.result().undoOffer).toBeNull()
  })

  it('healthy control: a full Task advancing once settles and offers Undo', async () => {
    const harness = renderHook({
      mutateOnce: vi.fn(async (_intent: TaskStatusIntent) =>
        receipt(makeTask({ status: 'started', revision: 8 })),
      ),
    })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('idle')
    expect(harness.result().undoOffer?.atRevision).toBe(8)
  })

  it('healthy control: the declared OPTIONAL Task fields may be present or absent', async () => {
    const rich = makeTask({
      status: 'started',
      revision: 8,
      created: '2026-09-01T00:00:00Z',
      updated_at: '2026-09-02T00:00:00Z',
      scheduled: '2026-09-03',
      estimate_minutes: 45,
      key_result_refs: [{ objective_id: 'O-1', key_result_id: 'K1' }],
    })
    const harness = renderHook({ mutateOnce: vi.fn(async (_intent: TaskStatusIntent) => receipt(rich)) })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('idle')
    expect(harness.result().undoOffer?.atRevision).toBe(8)
  })
})

describe('UI2 a receipt-only no-op is settled but not reversible', () => {
  it('offers no Undo and refuses to write when the receipt revision did not advance', async () => {
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) =>
      receipt(makeTask({ status: 'started', revision: 7 })),
    )
    const harness = renderHook({ mutateOnce })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('idle')
    expect(harness.result().undoOffer).toBeNull()

    act(() => harness.result().undoLast())
    await flush()

    // A no-op receipt must never produce an inverse mutation.
    expect(mutateOnce).toHaveBeenCalledTimes(1)
  })
})

describe('UI3 authoritative chronology gates NEW Undo ownership', () => {
  it('treats a replayed old receipt as historical when the current Task has moved on', async () => {
    const pendingCall = deferred()
    const harness = renderHook({
      mutateOnce: vi.fn((_intent: TaskStatusIntent) => pendingCall.promise),
    })

    act(() => harness.result().markInProgress())
    // External authority advances the SAME Task well past the pending intent.
    harness.rerender({ currentTask: makeTask({ status: 'done', revision: 15 }) })

    await act(async () => {
      pendingCall.settle(receipt(makeTask({ status: 'started', revision: 8 }), true))
      await Promise.resolve()
    })

    expect(harness.result().undoOffer).toBeNull()
    expect(harness.result().phase).toBe('idle')
  })

  it('healthy control: an already owned Undo keeps its captured revision when current state advances', async () => {
    const mutateOnce = vi
      .fn<(intent: TaskStatusIntent) => Promise<TaskStatusIntentResponse>>()
      .mockResolvedValueOnce(receipt(makeTask({ status: 'started', revision: 8 })))
      .mockResolvedValueOnce({ status: 409, body: {} } as unknown as TaskStatusIntentResponse)
    const reconcile = vi.fn()
    const harness = renderHook({ mutateOnce, reconcile })

    act(() => harness.result().markInProgress())
    await flush()
    expect(harness.result().undoOffer?.atRevision).toBe(8)

    harness.rerender({ currentTask: makeTask({ status: 'started', revision: 10 }) })
    expect(harness.result().undoOffer?.atRevision).toBe(8)

    act(() => harness.result().undoLast())
    await flush()

    const undoIntent = mutateOnce.mock.calls[1][0]
    // No rebase onto revision 10.
    expect(undoIntent.body).toEqual({ status: 'open', revision: 8 })
    expect(harness.result().phase).toBe('conflict')
    expect(reconcile).toHaveBeenCalledTimes(1)
  })
})

describe('UI4 a completion may only touch the slot it was dispatched into', () => {
  it('an old completion never clears a newer pending attempt or admits a third mutation', async () => {
    const first = deferred()
    const second = deferred()
    const calls: TaskStatusIntent[] = []
    const mutateOnce = vi.fn((intent: TaskStatusIntent) => {
      calls.push(intent)
      return calls.length === 1 ? first.promise : second.promise
    })
    // A correct caller: only the CURRENT epoch is current.
    let currentEpoch = 'epoch-A'
    const isCurrent = vi.fn((candidate: TaskStatusIntentOwner) => candidate.ownerEpoch === currentEpoch)
    const harness = renderHook({ mutateOnce, isCurrent })

    act(() => harness.result().markInProgress())
    currentEpoch = 'epoch-B'
    harness.rerender({
      owner: { ...OWNER, ownerEpoch: 'epoch-B' },
      currentTask: makeTask({ revision: 7 }),
    })
    act(() => harness.result().markInProgress())
    expect(mutateOnce).toHaveBeenCalledTimes(2)
    harness.rerender({})
    expect(harness.result().pending).toBe(true)

    await act(async () => {
      first.settle(receipt(makeTask({ status: 'started', revision: 8 })))
      await Promise.resolve()
    })

    // B is still outstanding: its slot was not released by A.
    harness.rerender({})
    expect(harness.result().pending).toBe(true)
    act(() => harness.result().markInProgress())
    expect(mutateOnce).toHaveBeenCalledTimes(2)

    await act(async () => {
      second.settle(receipt(makeTask({ status: 'started', revision: 8 })))
      await Promise.resolve()
    })
  })

  it('an old success cannot create Undo after an A to B to A round trip', async () => {
    const pendingCall = deferred()
    const harness = renderHook({
      mutateOnce: vi.fn((_intent: TaskStatusIntent) => pendingCall.promise),
    })

    act(() => harness.result().markInProgress())
    harness.rerender({ owner: { ...OWNER, ownerEpoch: 'epoch-B' } })
    harness.rerender({ owner: { ...OWNER, ownerEpoch: 'epoch-A' } })

    await act(async () => {
      pendingCall.settle(receipt(makeTask({ status: 'started', revision: 8 })))
      await Promise.resolve()
    })

    expect(harness.result().undoOffer).toBeNull()
  })

  it('an old loss cannot resurrect a retryable frozen intent after A to B to A', async () => {
    const pendingCall = deferred()
    const mutateOnce = vi.fn((_intent: TaskStatusIntent) => pendingCall.promise)
    const harness = renderHook({ mutateOnce })

    act(() => harness.result().markInProgress())
    harness.rerender({ owner: { ...OWNER, ownerEpoch: 'epoch-B' } })
    harness.rerender({ owner: { ...OWNER, ownerEpoch: 'epoch-A' } })

    await act(async () => {
      pendingCall.fail(new Error('connection lost'))
      await Promise.resolve()
    })

    expect(harness.result().retryable).toBe(false)
    act(() => harness.result().retry())
    await flush()
    expect(mutateOnce).toHaveBeenCalledTimes(1)
  })
})


describe('UIC-F1 the declared Task domain is checked to its elements and enums', () => {
  const started = (patch: Record<string, unknown>) =>
    ({ ...makeTask({ status: 'started', revision: 8 }), ...patch }) as unknown as Task

  it.each([
    ['an out-of-domain priority', { priority: 'P9' }],
    ['a non-string tag', { tags: [false] }],
    ['a non-string objective id', { objective_ids: [42] }],
    ['a null dependency', { dependencies: [null] }],
    ['a Subtask missing its title', { subtasks: [{ id: 'S-1' }] }],
    ['a null Subtask', { subtasks: [null] }],
    ['a Subtask with an out-of-domain status', { subtasks: [{ id: 'S-1', title: 'S', status: 'nope' }] }],
    ['a Subtask with an out-of-domain priority', { subtasks: [{ id: 'S-1', title: 'S', priority: 'P9' }] }],
    ['a Note with non-string text', { notes: [{ text: 7 }] }],
    ['a Note with a non-string date', { notes: [{ text: 'ok', date: 7 }] }],
    ['a null Note', { notes: [null] }],
    ['a false key result ref', { key_result_refs: [false] }],
    ['a key result ref missing its key', { key_result_refs: [{ objective_id: 'O-1' }] }],
    ['a key result ref with a non-string key', { key_result_refs: [{ objective_id: 'O-1', key_result_id: 3 }] }],
    ['a NaN estimate', { estimate_minutes: Number.NaN }],
    ['a fractional estimate', { estimate_minutes: 1.5 }],
    ['an out-of-bound estimate', { estimate_minutes: 1441 }],
  ])('keeps a receipt carrying %s unknown with the same frozen retry', async (_name, patch) => {
    const mutateOnce = vi.fn(async (_intent: TaskStatusIntent) => receipt(started(patch)))
    const newKey = vi.fn((kind: string) => `key-${kind}-1`)
    const harness = renderHook({ mutateOnce, newKey })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('unknown')
    expect(harness.result().retryable).toBe(true)
    expect(harness.result().undoOffer).toBeNull()

    act(() => harness.result().retry())
    await flush()

    // The same frozen object, and no second key.
    expect(mutateOnce).toHaveBeenCalledTimes(2)
    expect(mutateOnce.mock.calls[1][0]).toBe(mutateOnce.mock.calls[0][0])
    expect(newKey).toHaveBeenCalledTimes(1)
  })

  it('healthy control: populated legal collections and boundary estimates settle', async () => {
    const populated = started({
      subtasks: [
        { id: 'S-1', title: 'First', status: 'open', priority: 'P1' },
        { id: 'S-2', title: 'Second' },
      ],
      notes: [{ text: 'Recorded' }, { text: 'Dated', date: '2026-09-01' }],
      key_result_refs: [{ objective_id: 'O-1', key_result_id: 'K1' }],
      tags: ['alpha', 'beta'],
      objective_ids: ['O-1'],
      dependencies: ['T-9'],
      estimate_minutes: 1,
    })
    const harness = renderHook({ mutateOnce: vi.fn(async (_intent: TaskStatusIntent) => receipt(populated)) })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('idle')
    expect(harness.result().undoOffer?.atRevision).toBe(8)
  })

  it.each([
    ['the upper boundary estimate', 1440],
    ['a null estimate', null],
  ])('healthy control: %s is legal', async (_name, estimate_minutes) => {
    const harness = renderHook({
      mutateOnce: vi.fn(async (_intent: TaskStatusIntent) => receipt(started({ estimate_minutes }))),
    })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('idle')
  })

  it('healthy control: omitted optionals and opaque unknown metadata stay acceptable', async () => {
    const opaque = started({ server_only_field: { nested: true }, subtasks: [{ id: 'S-1', title: 'S', extra: 1 }] })
    const harness = renderHook({ mutateOnce: vi.fn(async (_intent: TaskStatusIntent) => receipt(opaque)) })

    act(() => harness.result().markInProgress())
    await flush()

    expect(harness.result().phase).toBe('idle')
    expect(harness.result().undoOffer?.atRevision).toBe(8)
  })
})

describe('UIC-F2 the exposed state belongs to the CURRENT lifetime', () => {
  it('renders pending for B from B own public action, with no extra caller rerender', async () => {
    const first = deferred()
    const second = deferred()
    const seen: TaskStatusIntent[] = []
    const mutateOnce = vi.fn((intent: TaskStatusIntent) => {
      seen.push(intent)
      return seen.length === 1 ? first.promise : second.promise
    })
    let currentEpoch = 'epoch-A'
    const isCurrent = vi.fn((candidate: TaskStatusIntentOwner) => candidate.ownerEpoch === currentEpoch)
    const harness = renderHook({ mutateOnce, isCurrent })

    act(() => harness.result().markInProgress())
    currentEpoch = 'epoch-B'
    // Exactly ONE owner rerender, then B's own public callback.
    harness.rerender({ owner: { ...OWNER, ownerEpoch: 'epoch-B' }, currentTask: makeTask() })
    act(() => harness.result().markInProgress())

    expect(mutateOnce).toHaveBeenCalledTimes(2)
    // Reactive public state, observed with no unrelated rerender.
    expect(harness.result().pending).toBe(true)
    expect(harness.result().phase).toBe('pending')

    // Duplicate suppression still holds for the current lifetime.
    act(() => harness.result().markInProgress())
    expect(mutateOnce).toHaveBeenCalledTimes(2)

    await act(async () => {
      first.settle(receipt(makeTask({ status: 'started', revision: 8 })))
      second.settle(receipt(makeTask({ status: 'started', revision: 8 })))
      await Promise.resolve()
    })
    // The old A completion was harmless.
    expect(mutateOnce).toHaveBeenCalledTimes(2)
  })

  it('exposes a fresh idle and eligible B after an unknown A, with no extra caller rerender', async () => {
    const mutateOnce = vi
      .fn<(intent: TaskStatusIntent) => Promise<TaskStatusIntentResponse>>()
      .mockRejectedValueOnce(new Error('connection lost'))
      .mockResolvedValueOnce(receipt(makeTask({ status: 'started', revision: 8 })))
    const harness = renderHook({ mutateOnce })

    act(() => harness.result().markInProgress())
    await flush()
    expect(harness.result().phase).toBe('unknown')

    harness.rerender({ owner: { ...OWNER, ownerEpoch: 'epoch-B' }, currentTask: makeTask() })

    expect(harness.result().phase).toBe('idle')
    expect(harness.result().retryable).toBe(false)
    expect(harness.result().canMarkInProgress).toBe(true)

    act(() => harness.result().markInProgress())
    await flush()

    expect(mutateOnce).toHaveBeenCalledTimes(2)
    expect(harness.result().undoOffer?.atRevision).toBe(8)
  })
})
