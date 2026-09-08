import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import { MultiProfileConnectionCenter } from './MultiProfileConnectionCenter'
import { connectionRegistryHostMessageSchema } from './connectionRegistryHostBridge'

/**
 * Activation failure and retry proof.
 *
 * The real component, Dialog and host bridge run; only the native WebView
 * message boundary is substituted, and the fixture keeps a real listener SET so
 * close, reopen and unmount are observable rather than asserted through a spy.
 *
 * Every case reads the COMPLETE outbound sequence, so an automatic activation
 * retry, a silent write or a second reload cannot hide behind a count.
 */

const TIMEOUT_MS = 20_000

interface WebViewMessageEvent extends Event { data?: unknown }
interface HostRequest { request_id: string; operation: string; [key: string]: any }

const activeId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const otherId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const workspaceId = '11111111-1111-4111-8111-111111111111'
const otherWorkspaceId = '55555555-5555-4555-8555-555555555555'
const firstProof = '22222222-2222-4222-8222-222222222222'
const secondProof = '66666666-6666-4666-8666-666666666666'

const digestA = `sha256:${'a'.repeat(64)}`
const digestB = `sha256:${'b'.repeat(64)}`
const digestC = `sha256:${'c'.repeat(64)}`

const activeProfile = {
  profile_id: activeId,
  label: 'Local planning',
  kind: 'local' as const,
  enabled: true,
  live_updates: true,
  expected_workspace_id: workspaceId,
  data_dir: 'C:/WorkStack/planning-ssot',
}

const otherProfile = {
  profile_id: otherId,
  label: 'Archive remote',
  kind: 'ssh' as const,
  enabled: true,
  live_updates: true,
  expected_workspace_id: otherWorkspaceId,
  ssh_host_alias: 'archive-linux',
  remote_app_dir: '/srv/archive/app',
  remote_data_dir: '/srv/archive/ssot',
  preferred_forward_port: 18_766,
  remote_port: 8_766,
}

const registryA = { schema_version: 1 as const, active_profile_id: activeId, profiles: [activeProfile] }

/**
 * A reload must be distinguishable by CONTENT, not only by its digest: the
 * reloaded snapshot carries a record the first one never had.
 */
const registryB = {
  schema_version: 1 as const,
  active_profile_id: activeId,
  profiles: [activeProfile, otherProfile],
}

/** The same reload, but with the host renaming the record being edited. */
const registryRenamed = {
  schema_version: 1 as const,
  active_profile_id: activeId,
  profiles: [{ ...activeProfile, label: 'Renamed by host' }, otherProfile],
}

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

function failure(request: { request_id: string; operation: string }, code: string, message: string) {
  const payload = {
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: request.request_id,
    operation: request.operation,
    ok: false,
    error: { code, message },
  }
  const parsed = connectionRegistryHostMessageSchema.safeParse(payload)
  if (!parsed.success) throw new Error(JSON.stringify(parsed.error.issues))
  return payload
}

const BRIDGE_OPERATIONS = [
  'get-registry',
  'save-registry',
  'discover-ssh-aliases',
  'choose-local-directory',
  'test-profile',
  'activate-profile',
] as const

/**
 * The COMPLETE outbound sequence, in order. An automatic activate-profile
 * retry, an unrequested save-registry or a second automatic reload fails here.
 */
function assertSequence(host: Host, expected: readonly string[]) {
  const actual = host.requests().map((request) => request.operation)
  expect(actual).toEqual([...expected])
  for (const operation of actual) expect(BRIDGE_OPERATIONS).toContain(operation)
}

function reloadRegistry(host: Host, registry: unknown, digest: string) {
  host.receive(success(lastRequest(host, 'get-registry'), { registry, registry_digest: digest }))
}

function openLoaded(host: Host, digest = digestA) {
  const view = render(<MultiProfileConnectionCenter activationEnabled onClose={vi.fn()} open enabled />)
  reloadRegistry(host, registryA, digest)
  return view
}

function activateButton() {
  return screen.getByRole('button', { name: 'Save and activate after restart' })
}

function saveButton() {
  return screen.getByRole('button', { name: 'Save profile' })
}

function labelInput() {
  return screen.getByLabelText('Profile label') as HTMLInputElement
}

/** Test the loaded active profile, then explicitly activate it. */
function testAndActivate(host: Host, digest: string, proofId: string): HostRequest {
  act(() => { fireEvent.click(screen.getByRole('button', { name: 'Test connection' })) })
  const test = lastRequest(host, 'test-profile')
  expect(test.base_registry_digest).toBe(digest)
  host.receive(success(test, {
    profile_id: test.profile.profile_id,
    kind: 'local',
    status: 'ready',
    actual_workspace_id: workspaceId,
    product_version: '1.0.8',
    protocol_version: 1,
    proof_id: proofId,
  }))
  expect(activateButton()).toBeEnabled()
  act(() => { fireEvent.click(activateButton()) })
  const activate = lastRequest(host, 'activate-profile')
  expect(activate.expected_registry_digest).toBe(digest)
  expect(activate.proof_id).toBe(proofId)
  return activate
}

function useTimers() {
  vi.useFakeTimers()
}

function advance(ms: number) {
  act(() => { vi.advanceTimersByTime(ms) })
}

afterEach(() => {
  vi.useRealTimers()
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
  vi.restoreAllMocks()
})

// --- AR01 ------------------------------------------------------------------

const ACTIVATION_REFUSALS = [
  ['activation_ambiguous', 'Several unconfirmed activations match this profile'],
  ['activation_conflict', 'Another unconfirmed activation targets this connection state with an unknown outcome.'],
  ['activation_unconfirmed', 'An earlier connection activation is still unconfirmed'],
  ['activation_manual_review', 'The stored connection activation records need manual review'],
] as const

for (const [code, summary] of ACTIVATION_REFUSALS) {
  test(`${code} states the refusal in its own copy, reloads once and never retries`, () => {
    const host = installHost()
    openLoaded(host)
    const activate = testAndActivate(host, digestA, firstProof)

    host.receive(failure(activate, code, 'ACTIVATION REFUSED ssh -i C:/secret/id_rsa token=leak-me'))

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(code)
    expect(alert).toHaveTextContent(summary)
    expect(alert).toHaveTextContent('This activation was refused and nothing was written.')
    expect(alert).not.toHaveTextContent('ssh_test_failed')
    expect(alert).not.toHaveTextContent('The SSH profile could not be verified.')
    const rendered = document.body.textContent ?? ''
    expect(rendered).not.toContain('C:/secret/id_rsa')
    expect(rendered).not.toContain('token=leak-me')
    // No success claim, and no blanket "try again".
    expect(rendered).not.toMatch(/Profile saved|Restart Work Stack to activate/)
    expect(rendered).not.toMatch(/timed out\. Try again/)
    // Exactly one automatic reload, and never a second activate-profile.
    assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry'])

    reloadRegistry(host, registryB, digestB)

    // The refusal survives the reload, and the invalidated proof is not restored.
    expect(screen.getByRole('alert')).toHaveTextContent(code)
    expect(screen.getByRole('status')).toHaveTextContent(
      'The connection registry was reloaded. Test this profile again before activating.',
    )
    expect(activateButton()).toBeDisabled()
    expect(saveButton()).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Test connection' })).toBeEnabled()
    assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry'])
  })
}

// --- AR02 ------------------------------------------------------------------

test('an unanswered activation reports an unknown outcome, reloads once and never retries', () => {
  useTimers()
  const host = installHost()
  openLoaded(host)
  testAndActivate(host, digestA, firstProof)

  advance(TIMEOUT_MS - 1)
  expect(screen.queryByRole('alert')).toBeNull()
  advance(1)

  const alert = screen.getByRole('alert')
  expect(alert).toHaveTextContent('The activation result is unknown: the desktop service did not answer,')
  expect(alert).toHaveTextContent('so Work Stack cannot tell whether this profile was activated.')
  const rendered = document.body.textContent ?? ''
  // An unanswered activation is neither a success nor a known failure.
  expect(rendered).not.toMatch(/Profile saved|Restart Work Stack to activate/)
  expect(rendered).not.toMatch(/was refused|nothing was written|could not be verified/i)
  expect(rendered).not.toMatch(/timed out\. Try again/)
  assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry'])

  reloadRegistry(host, registryB, digestB)

  expect(screen.getByRole('alert')).toHaveTextContent('The activation result is unknown')
  expect(activateButton()).toBeDisabled()
  expect(saveButton()).toBeDisabled()
  // The deadline of the reload is its own, and it is already answered.
  advance(TIMEOUT_MS)
  assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry'])
})

// --- AR03 ------------------------------------------------------------------

test('a reload that lands on an edited draft keeps the authored profile and stays locked', () => {
  const host = installHost()
  openLoaded(host)
  const activate = testAndActivate(host, digestA, firstProof)
  host.receive(failure(activate, 'activation_conflict', 'Another unconfirmed activation.'))

  // The editor stays usable while the automatic reload is in flight.
  expect(labelInput()).toHaveValue('Local planning')
  act(() => { fireEvent.change(labelInput(), { target: { value: 'Authored while reloading' } }) })
  expect(screen.getByRole('button', { name: 'Test connection' })).toBeDisabled()

  // The reload renames the very record being edited.
  reloadRegistry(host, registryRenamed, digestB)

  expect(labelInput()).toHaveValue('Authored while reloading')
  expect(screen.getByText('Renamed by host')).toBeVisible()
  expect(screen.getByText('Unsaved changes')).toBeVisible()
  expect(activateButton()).toBeDisabled()
  expect(saveButton()).toBeDisabled()
  assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry'])
})

// --- AR04 ------------------------------------------------------------------

test('another activation needs a fresh test bound to the reloaded digest', () => {
  const host = installHost()
  openLoaded(host)
  const first = testAndActivate(host, digestA, firstProof)
  host.receive(failure(first, 'activation_conflict', 'Another unconfirmed activation.'))
  reloadRegistry(host, registryB, digestB)

  expect(activateButton()).toBeDisabled()

  const second = testAndActivate(host, digestB, secondProof)

  expect(second.request_id).not.toBe(first.request_id)
  expect(second.expected_registry_digest).toBe(digestB)
  // The active profile retries through the activation core, not a metadata save.
  expect(second.profile_id).toBe(activeId)
  expect(second.registry.active_profile_id).toBe(activeId)
  expect(host.of('save-registry')).toHaveLength(0)
  assertSequence(host, [
    'get-registry', 'test-profile', 'activate-profile', 'get-registry', 'test-profile', 'activate-profile',
  ])

  host.receive(success(second, { registry: registryB, registry_digest: digestC, restart_required: true }))
  expect(screen.getByRole('status')).toHaveTextContent('Profile saved. Restart Work Stack to activate this workspace.')
})

// --- AR05 ------------------------------------------------------------------

for (const shape of ['refused reload', 'unanswered reload'] as const) {
  test(`a reload that does not land starts no further reload (${shape})`, () => {
    useTimers()
    const host = installHost()
    openLoaded(host)
    const first = testAndActivate(host, digestA, firstProof)
    host.receive(failure(first, 'activation_manual_review', 'Manual review required.'))
    const reload = lastRequest(host, 'get-registry')

    if (shape === 'refused reload') {
      host.receive(failure(reload, 'operation_failed', 'Connection registry operation failed.'))
    } else {
      advance(TIMEOUT_MS)
    }

    expect(screen.getByRole('status')).toHaveTextContent(
      'The connection registry was not reloaded. Close and reopen this center before activating.',
    )
    assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry'])
    // A reload that timed out keeps the activation outcome it was recovering.
    if (shape === 'unanswered reload') {
      expect(screen.getByRole('alert')).toHaveTextContent('activation_manual_review')
    }
    expect(activateButton()).toBeDisabled()
    advance(TIMEOUT_MS)
    assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry'])

    // The bound is one reload per failed activation, not one per session.
    const second = testAndActivate(host, digestA, secondProof)
    host.receive(failure(second, 'activation_conflict', 'Another unconfirmed activation.'))
    assertSequence(host, [
      'get-registry', 'test-profile', 'activate-profile', 'get-registry',
      'test-profile', 'activate-profile', 'get-registry',
    ])
  })
}

// --- AR06 ------------------------------------------------------------------

test('a reload reply that arrives after close or unmount changes nothing', () => {
  const host = installHost()
  const view = openLoaded(host)
  const activate = testAndActivate(host, digestA, firstProof)
  host.receive(failure(activate, 'activation_conflict', 'Another unconfirmed activation.'))
  const reload = lastRequest(host, 'get-registry')
  const disposed = [...host.listeners]
  expect(disposed.length).toBeGreaterThanOrEqual(1)
  const late = success(reload, { registry: registryRenamed, registry_digest: digestB })

  view.rerender(<MultiProfileConnectionCenter activationEnabled onClose={vi.fn()} open={false} enabled />)
  expect(host.listeners.size).toBe(0)

  host.receive(late)
  for (const listener of disposed) host.receiveVia(listener, late)
  expect(screen.queryByRole('dialog')).toBeNull()
  assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry'])

  view.rerender(<MultiProfileConnectionCenter activationEnabled onClose={vi.fn()} open enabled />)
  assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry', 'get-registry'])
  // The reopened read is a user-visible load, not the recovery reload.
  expect(screen.getByText('Reading the connection registry…')).toBeVisible()

  reloadRegistry(host, registryB, digestC)
  expect(labelInput()).toHaveValue('Local planning')
  expect(activateButton()).toBeDisabled()

  const stale = [...host.listeners]
  view.unmount()
  expect(host.listeners.size).toBe(0)
  for (const listener of [...disposed, ...stale]) host.receiveVia(listener, late)
  assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry', 'get-registry'])
})

function readyTestResult(proofId: string) {
  return {
    profile_id: activeId,
    kind: 'local' as const,
    status: 'ready' as const,
    actual_workspace_id: workspaceId,
    product_version: '1.0.8',
    protocol_version: 1,
    proof_id: proofId,
  }
}

function interceptPost(host: Host, handler: (request: HostRequest) => void) {
  host.postMessage.mockImplementation((serialized: string) => {
    handler(JSON.parse(String(serialized)) as HostRequest)
  })
}

// --- AR07 ------------------------------------------------------------------

test('a completed synchronous recovery reply survives a later throw from the same send', () => {
  const host = installHost()
  openLoaded(host)
  const activate = testAndActivate(host, digestA, firstProof)

  interceptPost(host, (request) => {
    if (request.operation !== 'get-registry') return
    host.postMessage.mockImplementation(() => undefined)
    host.receive(success(request, { registry: registryB, registry_digest: digestB }))
    throw new Error('post returned an error after the correlated reply')
  })
  host.receive(failure(activate, 'activation_conflict', 'sanitized'))

  fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  expect(lastRequest(host, 'test-profile').base_registry_digest).toBe(digestB)
  expect(screen.getByRole('status')).toHaveTextContent(
    'The connection registry was reloaded. Test this profile again before activating.',
  )
  expect(screen.getByRole('alert')).toHaveTextContent('activation_conflict')
  expect(screen.getByRole('alert')).not.toHaveTextContent('post returned an error')
  assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry', 'test-profile'])
})

test('a completed synchronous recovery error survives a later throw from the same send', () => {
  const host = installHost()
  openLoaded(host)
  const activate = testAndActivate(host, digestA, firstProof)

  interceptPost(host, (request) => {
    if (request.operation !== 'get-registry') return
    host.postMessage.mockImplementation(() => undefined)
    host.receive(failure(request, 'operation_failed', 'Connection registry operation failed.'))
    throw new Error('post returned an error after the correlated reply')
  })
  host.receive(failure(activate, 'activation_conflict', 'sanitized'))

  expect(screen.getByRole('status')).toHaveTextContent(
    'The connection registry was not reloaded. Close and reopen this center before activating.',
  )
  expect(screen.getByRole('alert')).toHaveTextContent('activation_conflict')
  expect(screen.getByRole('alert')).not.toHaveTextContent('post returned an error')
  assertSequence(host, ['get-registry', 'test-profile', 'activate-profile', 'get-registry'])
})

test('a recovery send that never reaches the host keeps the activation refusal', () => {
  const host = installHost()
  openLoaded(host)
  const activate = testAndActivate(host, digestA, firstProof)
  const webview = (window as unknown as { chrome: { webview: Record<string, unknown> } }).chrome.webview
  Object.defineProperty(window, 'chrome', {
    configurable: true,
    value: {
      webview: {
        addEventListener: webview.addEventListener,
        removeEventListener: webview.removeEventListener,
      },
    },
  })
  host.receive(failure(activate, 'activation_conflict', 'sanitized'))

  expect(screen.getByRole('status')).toHaveTextContent(
    'The connection registry was not reloaded. Close and reopen this center before activating.',
  )
  expect(screen.getByRole('alert')).toHaveTextContent('activation_conflict')
  assertSequence(host, ['get-registry', 'test-profile', 'activate-profile'])
})

test('a later send throw does not retire a newer request of the same operation', () => {
  const host = installHost()
  openLoaded(host)
  interceptPost(host, (request) => {
    if (request.operation !== 'test-profile') return
    host.postMessage.mockImplementation(() => undefined)
    host.receive(success(request, readyTestResult(firstProof)))
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
    throw new Error('post returned an error after the correlated reply')
  })

  fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const tests = host.of('test-profile')
  expect(tests).toHaveLength(2)
  expect(screen.getByRole('button', { name: 'Test connection' })).toBeDisabled()
  expect(screen.queryByRole('alert')).toBeNull()

  host.receive(success(tests[1], readyTestResult(secondProof)))
  expect(activateButton()).toBeEnabled()
  expect(screen.queryByRole('alert')).toBeNull()
  assertSequence(host, ['get-registry', 'test-profile', 'test-profile'])
})
