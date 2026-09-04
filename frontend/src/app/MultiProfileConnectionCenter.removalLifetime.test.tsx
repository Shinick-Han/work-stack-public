import { act, fireEvent, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, expect, test, vi } from 'vitest'

import { MultiProfileConnectionCenter } from './MultiProfileConnectionCenter'
import { connectionRegistryHostMessageSchema } from './connectionRegistryHostBridge'

/**
 * Removal response-loss lifetime proof.
 *
 * The real component, Dialog and host bridge run; only the native WebView
 * message boundary and the confirmation answer are substituted. The fixture
 * keeps a real listener SET - addEventListener registers the exact callback and
 * removeEventListener removes that same callback - because close, reopen,
 * unmount and StrictMode disposal cannot be observed through a spy that only
 * records that removeEventListener was called.
 */

const TIMEOUT_MS = 20_000

interface WebViewMessageEvent extends Event { data?: unknown }
interface HostRequest { request_id: string; operation: string; [key: string]: any }

const activeId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const targetId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const survivorId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
const workspaceId = '11111111-1111-4111-8111-111111111111'

const digestA = `sha256:${'a'.repeat(64)}`
const digestB = `sha256:${'b'.repeat(64)}`
const digestC = `sha256:${'c'.repeat(64)}`
const digestD = `sha256:${'d'.repeat(64)}`

const activeProfile = {
  profile_id: activeId,
  label: 'Local planning',
  kind: 'local' as const,
  enabled: true,
  live_updates: true,
  expected_workspace_id: workspaceId,
  data_dir: 'C:/WorkStack/planning-ssot',
}

const targetProfile = {
  profile_id: targetId,
  label: 'Saved remote',
  kind: 'ssh' as const,
  enabled: true,
  live_updates: true,
  expected_workspace_id: '44444444-4444-4444-8444-444444444444',
  ssh_host_alias: 'work-linux',
  remote_app_dir: '/srv/workstack/app',
  remote_data_dir: '/srv/workstack/ssot',
  preferred_forward_port: 18765,
  remote_port: 8765,
}

const survivorProfile = {
  profile_id: survivorId,
  label: 'Archive remote',
  kind: 'ssh' as const,
  enabled: true,
  live_updates: true,
  expected_workspace_id: '55555555-5555-4555-8555-555555555555',
  ssh_host_alias: 'archive-linux',
  remote_app_dir: '/srv/archive/app',
  remote_data_dir: '/srv/archive/ssot',
  preferred_forward_port: 18766,
  remote_port: 8766,
}

const fullRegistry = {
  schema_version: 1 as const,
  active_profile_id: activeId,
  profiles: [activeProfile, targetProfile, survivorProfile],
}

const withoutTarget = { ...fullRegistry, profiles: [activeProfile, survivorProfile] }

/**
 * A reload must be distinguishable by CONTENT, not only by its digest: the
 * survivor carries changed valid metadata and the records are reordered, while
 * the active profile identity is untouched. A regression that recombined the
 * new digest with the old records would then fail these cases.
 */
const renamedSurvivor = {
  ...survivorProfile,
  label: 'Archive remote (reorganised)',
  remote_app_dir: '/srv/archive2/app',
}
const freshRegistry = {
  ...fullRegistry,
  profiles: [renamedSurvivor, activeProfile, targetProfile],
}
const freshWithoutTarget = { ...fullRegistry, profiles: [renamedSurvivor, activeProfile] }

/** A host whose listeners are a real set, so disposal is observable. */
function installHost() {
  const listeners = new Set<(event: WebViewMessageEvent) => void>()
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', {
    configurable: true,
    value: {
      webview: {
        addEventListener: (_type: string, next: (event: WebViewMessageEvent) => void) => {
          listeners.add(next)
        },
        removeEventListener: (_type: string, next: (event: WebViewMessageEvent) => void) => {
          listeners.delete(next)
        },
        postMessage,
      },
    },
  })
  return {
    postMessage,
    listeners,
    requests(): HostRequest[] {
      return postMessage.mock.calls.map(([message]) => JSON.parse(String(message)) as HostRequest)
    },
    of(operation: string): HostRequest[] {
      return this.requests().filter((request) => request.operation === operation)
    },
    receive(data: unknown) {
      act(() => {
        for (const listener of [...listeners]) listener({ data } as WebViewMessageEvent)
      })
    },
    /** Deliver only to a captured callback, even after cleanup removed it. */
    receiveVia(listener: (event: WebViewMessageEvent) => void, data: unknown) {
      act(() => listener({ data } as WebViewMessageEvent))
    },
  }
}

type Host = ReturnType<typeof installHost>

function lastRequest(host: Host, operation: string): HostRequest {
  const request = [...host.requests()].reverse().find((item) => item.operation === operation)
  if (!request) throw new Error(`Missing ${operation} request`)
  return request
}

function success(request: { request_id: string; operation: string }, result: unknown) {
  const message = {
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: request.request_id,
    operation: request.operation,
    ok: true,
    result,
  }
  const parsed = connectionRegistryHostMessageSchema.safeParse(message)
  if (!parsed.success) throw new Error(JSON.stringify(parsed.error.issues))
  return message
}

function failure(request: { request_id: string; operation: string }, message: string) {
  const payload = {
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: request.request_id,
    operation: request.operation,
    ok: false,
    error: { code: 'operation_failed', message },
  }
  const parsed = connectionRegistryHostMessageSchema.safeParse(payload)
  if (!parsed.success) throw new Error(JSON.stringify(parsed.error.issues))
  return payload
}

function loadRegistry(host: Host, registry: unknown, digest: string) {
  host.receive(success(lastRequest(host, 'get-registry'), {
    registry, registry_digest: digest,
  }))
}

function removeButton() {
  return screen.getByRole('button', { name: 'Remove Saved remote' })
}

function targetIsListed() {
  return screen.queryByRole('button', { name: 'Remove Saved remote' }) !== null
}

function unknownMessage() {
  return screen.queryByText(/The removal result is unknown/)
}

/** Every operation the real bridge can send, so none can hide behind a count. */
const BRIDGE_OPERATIONS = [
  'get-registry',
  'save-registry',
  'discover-ssh-aliases',
  'choose-local-directory',
  'test-profile',
  'activate-profile',
] as const

/**
 * The COMPLETE outbound sequence, in order. Anything the case did not name -
 * an extra read, a discover-ssh-aliases, a choose-local-directory, a
 * test-profile or an activate-profile - fails here. This observes the message
 * boundary only; it claims nothing about the native side.
 */
function assertSequence(host: Host, expected: readonly string[]) {
  const actual = host.requests().map((request) => request.operation)
  expect(actual).toEqual([...expected])
  for (const operation of actual) {
    expect(BRIDGE_OPERATIONS).toContain(operation)
  }
}

function operations(host: Host): string[] {
  return host.requests().map((request) => request.operation)
}

interface Snapshot {
  digest: string
  requestIds: string[]
  saves: number
  listed: boolean
}

function snapshot(host: Host): Snapshot {
  const saves = host.of('save-registry')
  return {
    digest: saves.length ? String(saves[saves.length - 1].expected_registry_digest) : '',
    requestIds: host.requests().map((request) => request.request_id),
    saves: saves.length,
    listed: targetIsListed(),
  }
}

/**
 * Confirm, click Remove and return the exact request the component sent. The
 * caller states the complete candidate it expects, so a case that reloaded a
 * different snapshot cannot silently keep asserting the original records.
 */
function confirmRemoval(
  host: Host,
  expectedDigest: string,
  expectedProfiles: readonly unknown[] = [activeProfile, survivorProfile],
  seenRequestIds: readonly string[] = [],
): HostRequest {
  const before = host.of('save-registry').length
  act(() => { fireEvent.click(removeButton()) })
  const saves = host.of('save-registry')
  expect(saves.length).toBe(before + 1)
  const request = saves[saves.length - 1]
  expect(request.expected_registry_digest).toBe(expectedDigest)
  expect(request.registry.active_profile_id).toBe(activeId)
  // Exact records AND their order, never a set comparison.
  expect(request.registry.profiles).toEqual([...expectedProfiles])
  expect(typeof request.request_id).toBe('string')
  expect(seenRequestIds).not.toContain(request.request_id)
  return request
}

function openLoaded(host: Host, digest = digestA) {
  const view = render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadRegistry(host, fullRegistry, digest)
  return view
}

function advance(ms: number) {
  act(() => { vi.advanceTimersByTime(ms) })
}

function useTimers() {
  // Deliberately NOT shouldAdvanceTime: the deadline boundary must be moved
  // only by an explicit advance, never by elapsed real time during typing.
  // fireEvent is used throughout, so no user-event timer bridge is needed and
  // the deadline moves only when a case advances it explicitly.
  vi.useFakeTimers()
}

afterEach(() => {
  vi.useRealTimers()
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
  vi.restoreAllMocks()
})

// --- RL01 ------------------------------------------------------------------

test('timely correlated removal succeeds before its deadline', () => {
  useTimers()
  const host = installHost()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  openLoaded(host)

  const request = confirmRemoval(host, digestA)
  // No optimistic removal: the profile stays until a valid matching answer.
  expect(targetIsListed()).toBe(true)

  advance(19_000)
  expect(unknownMessage()).toBeNull()

  host.receive(success(request, { registry: withoutTarget, registry_digest: digestB }))

  expect(targetIsListed()).toBe(false)
  expect(screen.getByRole('button', { name: 'Remove Archive remote' })).toBeVisible()
  expect(screen.getByRole('status')).toHaveTextContent(/Removed the saved connection profile/)
  expect(screen.getByRole('status')).toHaveTextContent(/running workspace has not changed/)
  expect(screen.getByLabelText('Profile label')).toHaveValue('Local planning')

  const before = operations(host)
  advance(TIMEOUT_MS + 5_000)
  expect(unknownMessage()).toBeNull()
  // The expired deadline of a finished request adds no traffic at all.
  expect(operations(host)).toEqual(before)
  assertSequence(host, ['get-registry', 'save-registry'])
})

// --- RL02 ------------------------------------------------------------------

test('lost removal becomes unknown exactly at twenty seconds without repetition', () => {
  useTimers()
  const host = installHost()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  openLoaded(host)

  confirmRemoval(host, digestA)

  advance(TIMEOUT_MS - 1)
  expect(unknownMessage()).toBeNull()
  expect(removeButton()).toBeDisabled()
  expect(targetIsListed()).toBe(true)

  advance(1)
  expect(unknownMessage()).not.toBeNull()
  expect(screen.queryByText(/Removed the saved connection profile/)).toBeNull()
  expect(targetIsListed()).toBe(true)

  const before = operations(host)
  advance(40_000)
  // No automatic retry, refresh, discovery, test or activation follows.
  expect(operations(host)).toEqual(before)
  assertSequence(host, ['get-registry', 'save-registry'])
})

// --- RL03 ------------------------------------------------------------------

for (const shape of ['success', 'error'] as const) {
  test(`late timed-out removal result is inert (${shape})`, () => {
    useTimers()
    const host = installHost()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    openLoaded(host)

    const request = confirmRemoval(host, digestA)
    advance(TIMEOUT_MS)
    expect(unknownMessage()).not.toBeNull()
    const before = snapshot(host)

    host.receive(shape === 'success'
      ? success(request, { registry: withoutTarget, registry_digest: digestB })
      : failure(request, 'Distinguishable late host failure'))

    expect(unknownMessage()).not.toBeNull()
    expect(targetIsListed()).toBe(true)
    expect(screen.queryByText(/Removed the saved connection profile/)).toBeNull()
    expect(screen.queryByText(/Distinguishable late host failure/)).toBeNull()
    expect(snapshot(host).saves).toBe(before.saves)

    // The digest the late reply carried was never installed: the next supported
    // request still names the digest the component actually holds, with the
    // original records and a fresh request id.
    const next = confirmRemoval(host, digestA, [activeProfile, survivorProfile], [request.request_id])
    expect(next.request_id).not.toBe(request.request_id)
    assertSequence(host, ['get-registry', 'save-registry', 'save-registry'])
  })
}

// --- RL04 ------------------------------------------------------------------

for (const outcome of ['native committed', 'native did not commit'] as const) {
  test(`close and reopen reconciles the unknown outcome before explicit next removal (${outcome})`, () => {
    useTimers()
    const host = installHost()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const view = openLoaded(host)

    const lost = confirmRemoval(host, digestA)
    advance(TIMEOUT_MS)
    expect(unknownMessage()).not.toBeNull()

    // A late reply before the reload is inert.
    host.receive(success(lost, { registry: withoutTarget, registry_digest: digestB }))
    expect(targetIsListed()).toBe(true)

    view.rerender(<MultiProfileConnectionCenter onClose={vi.fn()} open={false} enabled />)
    view.rerender(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)

    const savesBefore = host.of('save-registry').length
    const committed = outcome === 'native committed'
    // The reload differs in CONTENT as well as digest.
    loadRegistry(host, committed ? freshWithoutTarget : freshRegistry, digestC)
    expect(screen.getByRole('button', { name: 'Remove Archive remote (reorganised)' })).toBeVisible()

    // A late reply after the reload is inert, and its old records cannot be
    // recombined with the new digest.
    host.receive(success(lost, { registry: withoutTarget, registry_digest: digestB }))

    expect(targetIsListed()).toBe(!committed)
    expect(screen.queryByRole('button', { name: 'Remove Archive remote' })).toBeNull()
    expect(host.of('save-registry').length).toBe(savesBefore)

    if (!committed) {
      const retry = confirmRemoval(
        host, digestC, [renamedSurvivor, activeProfile], [lost.request_id],
      )
      expect(retry.request_id).not.toBe(lost.request_id)
      assertSequence(host, ['get-registry', 'save-registry', 'get-registry', 'save-registry'])
    } else {
      assertSequence(host, ['get-registry', 'save-registry', 'get-registry'])
    }
  })
}

// --- RL05 ------------------------------------------------------------------

for (const shape of ['old success', 'old error'] as const) {
  test(`an old result cannot finish a new pending removal (${shape})`, () => {
    useTimers()
    const host = installHost()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const view = openLoaded(host)

    const firstRead = lastRequest(host, 'get-registry')
    const lost = confirmRemoval(host, digestA)
    advance(TIMEOUT_MS)
    view.rerender(<MultiProfileConnectionCenter onClose={vi.fn()} open={false} enabled />)
    view.rerender(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
    loadRegistry(host, freshRegistry, digestC)

    const pending = confirmRemoval(
      host, digestC, [renamedSurvivor, activeProfile], [lost.request_id],
    )
    const before = snapshot(host)
    const sequenceBefore = operations(host)

    host.receive(shape === 'old success'
      ? success(lost, { registry: withoutTarget, registry_digest: digestB })
      : failure(lost, 'Stale host failure'))

    // An old get-registry reply cannot finish the new pending removal either.
    host.receive(success(
      { request_id: firstRead.request_id, operation: 'get-registry' },
      { registry: fullRegistry, registry_digest: digestA },
    ))

    expect(targetIsListed()).toBe(true)
    expect(screen.getByRole('button', { name: 'Remove Archive remote (reorganised)' })).toBeVisible()
    expect(screen.queryByText(/Removed the saved connection profile/)).toBeNull()
    expect(screen.queryByText(/Stale host failure/)).toBeNull()
    expect(snapshot(host).saves).toBe(before.saves)
    expect(operations(host)).toEqual(sequenceBefore)
    expect(removeButton()).toBeDisabled()

    // The new request keeps its own deadline, and only its own reply ends it.
    advance(TIMEOUT_MS - 1)
    expect(unknownMessage()).toBeNull()
    host.receive(success(pending, { registry: freshWithoutTarget, registry_digest: digestD }))
    expect(targetIsListed()).toBe(false)
    expect(screen.getByRole('status')).toHaveTextContent(/Removed the saved connection profile/)
    assertSequence(host, ['get-registry', 'save-registry', 'get-registry', 'save-registry'])
  })
}

// --- RL06 ------------------------------------------------------------------

for (const shape of ['old success', 'old error'] as const) {
  test(`disposed owner responses cannot affect a new center with the same profile IDs (${shape})`, () => {
    useTimers()
    const host = installHost()
    vi.spyOn(window, 'confirm').mockReturnValue(true)

    const first = render(
      <StrictMode>
        <MultiProfileConnectionCenter onClose={vi.fn()} open enabled />
      </StrictMode>,
    )
    // The real StrictMode setup runs twice; extra reads are not permitted.
    assertSequence(host, ['get-registry', 'get-registry'])
    loadRegistry(host, fullRegistry, digestA)

    const lost = confirmRemoval(host, digestA)
    assertSequence(host, ['get-registry', 'get-registry', 'save-registry'])
    const oldListeners = [...host.listeners]
    expect(oldListeners.length).toBeGreaterThanOrEqual(1)

    first.unmount()
    // Real disposal: the exact callbacks were removed from the listener set.
    expect(host.listeners.size).toBe(0)
    assertSequence(host, ['get-registry', 'get-registry', 'save-registry'])

    render(
      <StrictMode>
        <MultiProfileConnectionCenter onClose={vi.fn()} open enabled />
      </StrictMode>,
    )
    assertSequence(host, [
      'get-registry', 'get-registry', 'save-registry', 'get-registry', 'get-registry',
    ])
    // Same profile IDs, but a distinguishable newer snapshot.
    loadRegistry(host, freshRegistry, digestC)
    expect(screen.getByRole('button', { name: 'Remove Archive remote (reorganised)' })).toBeVisible()
    const before = snapshot(host)
    const sequenceBefore = operations(host)

    const stale = shape === 'old success'
      ? success(lost, { registry: withoutTarget, registry_digest: digestB })
      : failure(lost, 'Disposed owner failure')

    // Delivered on the current subscription...
    host.receive(stale)
    // ...and explicitly queued on a disposed callback whose pending map is gone.
    for (const listener of oldListeners) host.receiveVia(listener, stale)

    expect(targetIsListed()).toBe(true)
    // The remount kept the newer snapshot; no old record was reinstated.
    expect(screen.getByRole('button', { name: 'Remove Archive remote (reorganised)' })).toBeVisible()
    expect(screen.queryByText(/Removed the saved connection profile/)).toBeNull()
    expect(screen.queryByText(/Disposed owner failure/)).toBeNull()
    expect(unknownMessage()).toBeNull()
    expect(snapshot(host).saves).toBe(before.saves)
    expect(operations(host)).toEqual(sequenceBefore)

    // A fresh current result is still accepted by the new owner, and its
    // candidate carries the reloaded records in their reloaded order.
    const fresh = confirmRemoval(
      host, digestC, [renamedSurvivor, activeProfile], [lost.request_id],
    )
    const expectedSequence = [
      'get-registry', 'get-registry', 'save-registry',
      'get-registry', 'get-registry', 'save-registry',
    ]
    assertSequence(host, expectedSequence)
    host.receive(success(fresh, { registry: freshWithoutTarget, registry_digest: digestD }))
    expect(targetIsListed()).toBe(false)
    assertSequence(host, expectedSequence)
    advance(TIMEOUT_MS + 40_000)
    expect(targetIsListed()).toBe(false)
    expect(unknownMessage()).toBeNull()
    assertSequence(host, expectedSequence)
  })
}

// --- RL07 ------------------------------------------------------------------

test('a newly edited survivor after timeout is not overwritten by late removal success', () => {
  useTimers()
  const host = installHost()
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  const onClose = vi.fn()
  render(<MultiProfileConnectionCenter onClose={onClose} open enabled />)
  loadRegistry(host, fullRegistry, digestA)

  const lost = confirmRemoval(host, digestA)
  advance(TIMEOUT_MS)
  expect(unknownMessage()).not.toBeNull()

  const listEntry = screen
    .getAllByRole('button', { name: /Archive remote/ })
    .find((button) => !String(button.textContent).startsWith('Remove'))
  expect(listEntry).toBeDefined()
  act(() => { fireEvent.click(listEntry as HTMLElement) })
  const label = screen.getByLabelText('Profile label')
  act(() => { fireEvent.change(label, { target: { value: 'Archive remote edited' } }) })
  const before = snapshot(host)

  host.receive(success(lost, {
    registry: { ...withoutTarget, profiles: [activeProfile, { ...survivorProfile, label: 'Server chosen label' }] },
    registry_digest: digestB,
  }))

  expect(screen.getByLabelText('Profile label')).toHaveValue('Archive remote edited')
  expect(screen.queryByText(/Server chosen label/)).toBeNull()
  expect(targetIsListed()).toBe(true)
  expect(screen.queryByText(/Removed the saved connection profile/)).toBeNull()
  expect(snapshot(host).saves).toBe(before.saves)

  // Ordinary dirty-discard confirmation still governs Close after the late
  // reply: cancelling keeps the dialog and the edited draft.
  confirm.mockReturnValue(false)
  act(() => { fireEvent.click(screen.getByRole('button', { name: 'Close' })) })
  expect(onClose).not.toHaveBeenCalled()
  expect(screen.getByRole('dialog')).toBeVisible()
  expect(screen.getByLabelText('Profile label')).toHaveValue('Archive remote edited')
  expect(String(confirm.mock.calls[confirm.mock.calls.length - 1][0]))
    .toMatch(/unsaved connection profile changes/i)

  // Confirming the discard hands control back to the owner.
  confirm.mockReturnValue(true)
  act(() => { fireEvent.click(screen.getByRole('button', { name: 'Close' })) })
  expect(onClose).toHaveBeenCalledTimes(1)
  assertSequence(host, ['get-registry', 'save-registry'])
})

// --- RL08 ------------------------------------------------------------------

test('matching success that retains the target cannot claim removal', () => {
  useTimers()
  const host = installHost()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  openLoaded(host)

  const request = confirmRemoval(host, digestA)
  // Correlated and schema-valid, but the target is still present.
  host.receive(success(request, { registry: fullRegistry, registry_digest: digestB }))

  expect(targetIsListed()).toBe(true)
  expect(screen.queryByText(/Removed the saved connection profile/)).toBeNull()
  expect(screen.getByRole('alert')).toBeVisible()
  // The returned digest stays usable: a following genuine removal succeeds.
  const second = confirmRemoval(host, digestB, [activeProfile, survivorProfile], [request.request_id])
  host.receive(success(second, { registry: withoutTarget, registry_digest: digestC }))
  expect(targetIsListed()).toBe(false)
  assertSequence(host, ['get-registry', 'save-registry', 'save-registry'])
})
