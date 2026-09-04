import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'

import { MultiProfileConnectionCenter } from './MultiProfileConnectionCenter'
import { connectionRegistryHostMessageSchema } from './connectionRegistryHostBridge'

interface WebViewMessageEvent extends Event { data?: unknown }
interface HostRequest { request_id: string; operation: string; [key: string]: any }

const profileId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const workspaceId = '11111111-1111-4111-8111-111111111111'
const replacementWorkspaceId = '33333333-3333-4333-8333-333333333333'
const proofId = '22222222-2222-4222-8222-222222222222'
const registryDigest = `sha256:${'a'.repeat(64)}`
const localProfile = {
  profile_id: profileId,
  label: 'Local planning',
  kind: 'local' as const,
  enabled: true,
  live_updates: true,
  expected_workspace_id: workspaceId,
  data_dir: 'C:/WorkStack/planning-ssot',
}
const registry = { schema_version: 1 as const, active_profile_id: profileId, profiles: [localProfile] }

function installHost() {
  let listener: ((event: WebViewMessageEvent) => void) | undefined
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(),
    postMessage,
  } } })
  return {
    postMessage,
    requests(): HostRequest[] { return postMessage.mock.calls.map(([message]) => JSON.parse(message) as HostRequest) },
    receive(data: unknown) { act(() => listener?.({ data } as WebViewMessageEvent)) },
  }
}

function lastRequest(host: ReturnType<typeof installHost>, operation: string): HostRequest {
  const request = [...host.requests()].reverse().find((candidate) => candidate.operation === operation)
  if (!request) throw new Error(`Missing ${operation} request`)
  return request
}

function success(request: { request_id: string; operation: string }, result: unknown) {
  return {
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: request.request_id,
    operation: request.operation,
    ok: true,
    result,
  }
}

function loadRegistry(host: ReturnType<typeof installHost>) {
  const request = lastRequest(host, 'get-registry')
  const response = success(request, { registry, registry_digest: registryDigest })
  const parsed = connectionRegistryHostMessageSchema.safeParse(response)
  if (!parsed.success) throw new Error(JSON.stringify(parsed.error.issues))
  host.receive(response)
}

afterEach(() => {
  vi.useRealTimers()
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
  vi.restoreAllMocks()
})

test('times out a lost native response and unlocks the editor', () => {
  vi.useFakeTimers()
  const host = installHost()
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadRegistry(host)

  fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  expect(screen.getByRole('button', { name: 'Test connection' })).toBeDisabled()
  act(() => vi.advanceTimersByTime(20_000))

  expect(screen.getByText(/operation timed out/)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Test connection' })).toBeEnabled()
})

test('stays dark unless its release feature gate is explicitly enabled', () => {
  const host = installHost()
  const { rerender } = render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled={false} />)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(host.postMessage).not.toHaveBeenCalled()

  rerender(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  expect(screen.getByRole('dialog', { name: 'SSOT connections' })).toBeVisible()
  expect(host.requests()).toContainEqual(expect.objectContaining({ operation: 'get-registry' }))
})

test('lists exact profile authority details and exposes accessible local and SSH editors', async () => {
  const host = installHost()
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadRegistry(host)

  const list = screen.getByRole('list')
  expect(within(list).getByRole('button', { name: /Local planning/ })).toHaveTextContent('C:/WorkStack/planning-ssot')
  expect(within(list).getByRole('button', { name: /Local planning/ })).toHaveTextContent('Active · Enabled · planning-ssot')
  expect(within(list).getByRole('button', { name: /Local planning/ })).not.toHaveTextContent(workspaceId)
  expect(screen.getByLabelText('Profile label')).toHaveValue('Local planning')
  expect(screen.getByLabelText('Local SSOT directory')).toHaveValue('C:/WorkStack/planning-ssot')

  await userEvent.click(screen.getByRole('button', { name: 'Add SSH' }))
  const discovery = lastRequest(host, 'discover-ssh-aliases')
  host.receive(success(discovery, { aliases: ['work-linux', 'build-box'] }))
  expect(screen.getByLabelText('SSH host alias')).toHaveAttribute('list', 'workstack-ssh-aliases')
  expect(screen.getByText('Remote SSH SSOT')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Test connection' })).toBeDisabled()

  await userEvent.tab()
  expect(document.activeElement).not.toBe(document.body)
})

test('uses native Browse and ignores stale operation replies by request id', async () => {
  const host = installHost()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<MultiProfileConnectionCenter activationEnabled onClose={vi.fn()} open enabled />)
  loadRegistry(host)

  await userEvent.click(screen.getByRole('button', { name: 'Browse…' }))
  const browse = lastRequest(host, 'choose-local-directory')
  host.receive(success({ ...browse, request_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' }, { selection: 'C:/Wrong/ssot' }))
  expect(screen.getByLabelText('Local SSOT directory')).toHaveValue('C:/WorkStack/planning-ssot')
  host.receive(success(browse, { selection: 'C:/Selected/ssot' }))
  expect(screen.getByLabelText('Local SSOT directory')).toHaveValue('C:/Selected/ssot')
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const changedAuthorityTest = lastRequest(host, 'test-profile')
  host.receive(success(changedAuthorityTest, {
    profile_id: profileId, kind: 'local', status: 'ready', actual_workspace_id: workspaceId,
    product_version: null, protocol_version: null, proof_id: proofId,
  }))
  expect(screen.getByRole('button', { name: 'Save profile' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save and activate after restart' })).toBeEnabled()

  await userEvent.click(screen.getByRole('button', { name: 'Add SSH' }))
  const first = lastRequest(host, 'discover-ssh-aliases')
  host.receive(success(first, { aliases: ['old-host'] }))
  await userEvent.click(screen.getByRole('button', { name: 'Refresh SSH aliases' }))
  const second = lastRequest(host, 'discover-ssh-aliases')
  host.receive(success(first, { aliases: ['stale-host'] }))
  host.receive(success(second, { aliases: ['current-host'] }))
  expect(screen.getByText('SSH aliases loaded from your SSH config.')).toBeVisible()
  expect(document.querySelector('option[value="current-host"]')).not.toBeNull()
  expect(document.querySelector('option[value="stale-host"]')).toBeNull()
})

test('uses the selected directory name for a new local profile label', async () => {
  const host = installHost()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<MultiProfileConnectionCenter activationEnabled onClose={vi.fn()} open enabled />)
  loadRegistry(host)

  await userEvent.click(screen.getByRole('button', { name: 'Add local' }))
  await userEvent.click(screen.getByRole('button', { name: 'Browse…' }))
  const browse = lastRequest(host, 'choose-local-directory')
  host.receive(success(browse, { selection: 'C:/WorkStack/customer-alpha' }))

  expect(screen.getByLabelText('Profile label')).toHaveValue('customer-alpha')
  expect(screen.getByLabelText('Local SSOT directory')).toHaveValue('C:/WorkStack/customer-alpha')
})

test('does not let a late profile test bless a draft edited after the request', async () => {
  const host = installHost()
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadRegistry(host)

  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const request = lastRequest(host, 'test-profile')
  await userEvent.type(screen.getByLabelText('Profile label'), ' changed')
  host.receive(success(request, {
    profile_id: profileId,
    kind: 'local',
    status: 'ready',
    actual_workspace_id: workspaceId,
    product_version: '1.0.6',
    protocol_version: 1,
    proof_id: proofId,
  }))

  expect(screen.queryByText(/Connection test passed/)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save profile' })).toBeDisabled()
  expect(screen.getByText('Unsaved changes')).toBeVisible()
})

test('normalizes label whitespace consistently across Test correlation', async () => {
  const host = installHost()
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadRegistry(host)

  await userEvent.clear(screen.getByLabelText('Profile label'))
  await userEvent.type(screen.getByLabelText('Profile label'), '  Local planning  ')
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const request = lastRequest(host, 'test-profile')
  expect(request.profile.label).toBe('Local planning')
  expect(request.base_registry_digest).toBe(registryDigest)
  host.receive(success(request, {
    profile_id: profileId, kind: 'local', status: 'ready', actual_workspace_id: workspaceId,
    product_version: '1.0.6', protocol_version: 1, proof_id: proofId,
  }))

  expect(screen.getByText(/Connection test passed/)).toBeVisible()
})

test('separates metadata Save from restart activation and never claims a hot switch', async () => {
  const host = installHost()
  render(<MultiProfileConnectionCenter activationEnabled onClose={vi.fn()} open enabled />)
  loadRegistry(host)

  await userEvent.clear(screen.getByLabelText('Profile label'))
  await userEvent.type(screen.getByLabelText('Profile label'), 'Renamed local')
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const testRequest = lastRequest(host, 'test-profile')
  host.receive(success(testRequest, {
    profile_id: profileId,
    kind: 'local',
    status: 'ready',
    actual_workspace_id: workspaceId,
    product_version: null,
    protocol_version: null,
    proof_id: proofId,
  }))

  await userEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  const saveRequest = lastRequest(host, 'save-registry')
  expect(saveRequest.registry.profiles[0].label).toBe('Renamed local')
  expect(saveRequest.expected_registry_digest).toBe(registryDigest)
  const savedDigest = `sha256:${'b'.repeat(64)}`
  host.receive(success(saveRequest, { registry: saveRequest.registry, registry_digest: savedDigest }))
  expect(screen.getByText('Profile saved. The running workspace has not changed.')).toBeVisible()

  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const activationTestRequest = lastRequest(host, 'test-profile')
  host.receive(success(activationTestRequest, {
    profile_id: profileId, kind: 'local', status: 'ready', actual_workspace_id: workspaceId,
    product_version: null, protocol_version: null, proof_id: proofId,
  }))
  await userEvent.click(screen.getByRole('button', { name: 'Save and activate after restart' }))
  const activateRequest = lastRequest(host, 'activate-profile')
  expect(activateRequest.profile_id).toBe(profileId)
  expect(activateRequest).toEqual(expect.objectContaining({ proof_id: proofId, expected_registry_digest: savedDigest }))
  const activatedDigest = `sha256:${'c'.repeat(64)}`
  host.receive(success(activateRequest, {
    registry: activateRequest.registry,
    registry_digest: activatedDigest,
    restart_required: true,
  }))
  expect(screen.getByText('Profile saved. Restart Work Stack to activate this workspace.')).toBeVisible()
  expect(screen.queryByText(/switched now/i)).not.toBeInTheDocument()
})

test('fails closed when a detected identity differs from the saved workspace authority', async () => {
  const host = installHost()
  const onReviewSynchronization = vi.fn()
  render(<MultiProfileConnectionCenter activationEnabled onClose={vi.fn()} onReviewSynchronization={onReviewSynchronization} open enabled />)
  loadRegistry(host)

  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const mismatchTestRequest = lastRequest(host, 'test-profile')
  expect(mismatchTestRequest.profile.expected_workspace_id).toBe(workspaceId)
  host.receive(success(mismatchTestRequest, {
    profile_id: profileId,
    kind: 'local',
    status: 'identity_mismatch',
    actual_workspace_id: replacementWorkspaceId,
    product_version: '1.0.6',
    protocol_version: 1,
    proof_id: null,
  }))

  expect(screen.getByText('Saved profile identity').nextSibling).toHaveTextContent(workspaceId)
  expect(screen.getByText('Detected workspace identity').nextSibling).toHaveTextContent(replacementWorkspaceId)
  expect(screen.getByRole('alert')).toHaveTextContent('cannot prove whether the detected identity is a durable workspace authority')
  expect(screen.getByRole('button', { name: 'Save profile' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save and activate after restart' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Review workspace synchronization' }))
  expect(onReviewSynchronization).toHaveBeenCalledOnce()
  expect(host.requests().some((candidate) => candidate.operation === 'save-registry' || candidate.operation === 'activate-profile')).toBe(false)
})

test('does not imply a synchronization handoff when no callback is available', async () => {
  const host = installHost()
  render(<MultiProfileConnectionCenter activationEnabled onClose={vi.fn()} open enabled />)
  loadRegistry(host)

  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const request = lastRequest(host, 'test-profile')
  host.receive(success(request, {
    profile_id: profileId, kind: 'local', status: 'identity_mismatch',
    actual_workspace_id: replacementWorkspaceId, product_version: '1.0.6', protocol_version: 1, proof_id: null,
  }))

  expect(screen.getByRole('alert')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Review workspace synchronization' })).not.toBeInTheDocument()
})

test('rejects a legacy registry reply that omits CAS identity and emits no mutation', () => {
  const host = installHost()
  render(<MultiProfileConnectionCenter activationEnabled onClose={vi.fn()} open enabled />)
  const request = lastRequest(host, 'get-registry')
  host.receive(success(request, { registry }))

  expect(screen.getByRole('heading', { name: 'Loading profiles' })).toBeVisible()
  expect(host.requests().some((candidate) => candidate.operation === 'save-registry' || candidate.operation === 'activate-profile')).toBe(false)
})

test('protects dirty edits before close or profile replacement', async () => {
  const host = installHost()
  const onClose = vi.fn()
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  render(<MultiProfileConnectionCenter onClose={onClose} open enabled />)
  loadRegistry(host)

  await userEvent.type(screen.getByLabelText('Profile label'), ' draft')
  await userEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(confirm).toHaveBeenCalledWith('Discard unsaved connection profile changes?')
  expect(onClose).not.toHaveBeenCalled()

  await userEvent.click(screen.getByRole('button', { name: 'Add local' }))
  expect(screen.getByLabelText('Profile label')).toHaveValue('Local planning draft')
  confirm.mockReturnValue(true)
  await userEvent.click(screen.getByRole('button', { name: 'Add local' }))
  expect(screen.getByLabelText('Profile label')).toHaveValue('')
})


// --- T-0001 saved inactive profile removal ---------------------------------

const inactiveId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const inactiveProfile = {
  profile_id: inactiveId,
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
const twoProfileRegistry = {
  schema_version: 1 as const,
  active_profile_id: profileId,
  profiles: [localProfile, inactiveProfile],
}

function loadTwoProfiles(host: ReturnType<typeof installHost>) {
  const request = lastRequest(host, 'get-registry')
  host.receive(success(request, { registry: twoProfileRegistry, registry_digest: registryDigest }))
}

function removeButton() {
  return screen.getByRole('button', { name: 'Remove Saved remote' })
}

test('removal confirmation names the persisted profile and sends only the surviving profiles', async () => {
  const host = installHost()
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadTwoProfiles(host)

  await userEvent.click(removeButton())

  const prompt = String(confirm.mock.calls[0][0])
  expect(prompt).toContain('Saved remote')
  expect(prompt).toContain('work-linux:/srv/workstack/ssot')
  expect(prompt).toMatch(/only this connection entry is removed/i)
  expect(prompt).toMatch(/left untouched/i)

  const request = lastRequest(host, 'save-registry')
  expect(request.expected_registry_digest).toBe(registryDigest)
  expect(request.registry.active_profile_id).toBe(profileId)
  expect(request.registry.profiles).toEqual([localProfile])
})

test('cancelling the confirmation sends nothing and keeps the profile listed', async () => {
  const host = installHost()
  vi.spyOn(window, 'confirm').mockReturnValue(false)
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadTwoProfiles(host)

  await userEvent.click(removeButton())

  expect(host.requests().some((request) => request.operation === 'save-registry')).toBe(false)
  expect(removeButton()).toBeVisible()
})

test('never offers removal for the active profile', () => {
  const host = installHost()
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadTwoProfiles(host)

  expect(screen.queryByRole('button', { name: 'Remove Local planning' })).not.toBeInTheDocument()
  expect(removeButton()).toBeVisible()
})

test('disables removal while another registry request is in flight', async () => {
  const host = installHost()
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadTwoProfiles(host)

  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))

  expect(removeButton()).toBeDisabled()
})

test('a dirty draft requires a second confirmation and never persists the draft edit', async () => {
  const host = installHost()
  const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(true).mockReturnValueOnce(false)
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadTwoProfiles(host)
  await userEvent.type(screen.getByLabelText('Profile label'), ' edited')

  await userEvent.click(removeButton())

  expect(String(confirm.mock.calls[1][0])).toMatch(/unsaved connection profile changes will be discarded/i)
  expect(host.requests().some((request) => request.operation === 'save-registry')).toBe(false)

  confirm.mockReturnValue(true)
  await userEvent.click(removeButton())

  const request = lastRequest(host, 'save-registry')
  expect(request.registry.profiles).toEqual([localProfile])
  expect(JSON.stringify(request.registry)).not.toContain('edited')
})

test('reports the removal only after the correlated reply and resets the editor to the active profile', async () => {
  const host = installHost()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadTwoProfiles(host)

  await userEvent.click(removeButton())
  expect(screen.getByRole('button', { name: 'Remove Saved remote' })).toBeInTheDocument()

  const request = lastRequest(host, 'save-registry')
  host.receive(success(request, {
    registry: { ...twoProfileRegistry, profiles: [localProfile] },
    registry_digest: `sha256:${'b'.repeat(64)}`,
  }))

  expect(screen.queryByRole('button', { name: 'Remove Saved remote' })).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent(/Removed the saved connection profile/)
  expect(screen.getByRole('status')).toHaveTextContent(/running workspace has not changed/)
  expect(screen.getByLabelText('Profile label')).toHaveValue('Local planning')
  expect(host.requests().some((entry) => entry.operation === 'activate-profile')).toBe(false)
})

test('an error reply preserves the profile and shows the failure', async () => {
  const host = installHost()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadTwoProfiles(host)

  await userEvent.click(removeButton())
  const request = lastRequest(host, 'save-registry')
  host.receive({
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: request.request_id,
    operation: request.operation,
    ok: false,
    error: { code: 'registry_conflict', message: 'The connection registry changed. Reload and try again.' },
  })

  expect(screen.getByRole('alert')).toHaveTextContent('The connection registry changed. Reload and try again.')
  expect(removeButton()).toBeVisible()
})

test('ignores a reply whose request id does not match the pending removal', async () => {
  const host = installHost()
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<MultiProfileConnectionCenter onClose={vi.fn()} open enabled />)
  loadTwoProfiles(host)

  await userEvent.click(removeButton())
  const request = lastRequest(host, 'save-registry')
  host.receive(success({ request_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', operation: request.operation }, {
    registry: { ...twoProfileRegistry, profiles: [localProfile] },
    registry_digest: `sha256:${'c'.repeat(64)}`,
  }))

  expect(removeButton()).toBeVisible()
  expect(screen.queryByText(/Removed the saved connection profile/)).not.toBeInTheDocument()
})