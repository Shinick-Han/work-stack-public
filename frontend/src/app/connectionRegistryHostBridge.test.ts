import { afterEach, expect, test, vi } from 'vitest'

import {
  MAX_CONNECTION_REGISTRY_REQUEST_BYTES,
  MAX_CONNECTION_REGISTRY_RESPONSE_BYTES,
  connectionRegistryHostRequestSchema,
  connectionRegistryHostMessageSchema,
  activateConnectionProfile,
  requestConnectionProfileTest,
  requestConnectionRegistry,
  requestLocalDirectoryChoice,
  requestSshAliasDiscovery,
  saveConnectionRegistry,
  subscribeConnectionRegistryHostMessages,
} from './connectionRegistryHostBridge'
import { MAX_CONNECTION_PROFILES } from '../domain/connectionRegistrySchemas'

interface WebViewMessageEvent extends Event { data?: unknown }

const profileId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const workspaceId = '11111111-1111-4111-8111-111111111111'
const proofId = '22222222-2222-4222-8222-222222222222'
const registryDigest = `sha256:${'a'.repeat(64)}`
const registry = {
  schema_version: 1 as const,
  active_profile_id: profileId,
  profiles: [{
    profile_id: profileId,
    label: 'Work Linux',
    kind: 'ssh' as const,
    enabled: true,
    live_updates: true,
    expected_workspace_id: workspaceId,
    ssh_host_alias: 'work-linux',
    remote_app_dir: '/srv/workstack/app',
    remote_data_dir: '/srv/workstack/ssot',
    preferred_forward_port: 24_567,
    remote_port: 8_765,
  }],
}

function installHost() {
  let listener: ((event: WebViewMessageEvent) => void) | undefined
  const postMessage = vi.fn()
  const removeEventListener = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener,
    postMessage,
  } } })
  return {
    postMessage,
    removeEventListener,
    receive(data: unknown) { listener?.({ data } as WebViewMessageEvent) },
  }
}

afterEach(() => {
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
})

test('emits only bounded versioned registry, discovery, browse, test, and activation requests', () => {
  const host = installHost()
  const getRequestId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
  const saveRequestId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
  const aliasesRequestId = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
  expect(requestConnectionRegistry(getRequestId)).toBe(getRequestId)
  expect(saveConnectionRegistry(registry, registryDigest, saveRequestId)).toBe(saveRequestId)
  expect(requestSshAliasDiscovery(aliasesRequestId)).toBe(aliasesRequestId)
  expect(requestLocalDirectoryChoice('ffffffff-ffff-4fff-8fff-ffffffffffff')).toBe('ffffffff-ffff-4fff-8fff-ffffffffffff')
  expect(requestConnectionProfileTest({ ...registry.profiles[0], expected_workspace_id: null }, registryDigest, '99999999-9999-4999-8999-999999999999')).toBe('99999999-9999-4999-8999-999999999999')
  expect(activateConnectionProfile(registry, profileId, proofId, registryDigest, '88888888-8888-4888-8888-888888888888')).toBe('88888888-8888-4888-8888-888888888888')

  const requests = host.postMessage.mock.calls.map(([message]) => JSON.parse(message))
  expect(requests).toEqual([
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: getRequestId, operation: 'get-registry' },
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: saveRequestId, operation: 'save-registry', registry, expected_registry_digest: registryDigest },
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: aliasesRequestId, operation: 'discover-ssh-aliases' },
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: 'ffffffff-ffff-4fff-8fff-ffffffffffff', operation: 'choose-local-directory' },
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: '99999999-9999-4999-8999-999999999999', operation: 'test-profile', profile: { ...registry.profiles[0], expected_workspace_id: null }, base_registry_digest: registryDigest },
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: '88888888-8888-4888-8888-888888888888', operation: 'activate-profile', registry, profile_id: profileId, proof_id: proofId, expected_registry_digest: registryDigest },
  ])
  for (const [message] of host.postMessage.mock.calls) {
    expect(new TextEncoder().encode(message).byteLength).toBeLessThanOrEqual(MAX_CONNECTION_REGISTRY_REQUEST_BYTES)
  }
})

test('does not expose an outbound URL, raw SSH, filesystem discovery, or mutation operation', () => {
  installHost()
  for (const request of [
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: profileId, operation: 'discover-ssh-aliases', config_path: 'C:\\secrets' },
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: profileId, operation: 'ssh', command: 'ssh work-linux' },
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: profileId, operation: 'fetch', url: 'https://remote.invalid' },
    { type: 'workstack-connection-registry-request', schema_version: 1, request_id: profileId, operation: 'delete', profile_id: profileId },
  ]) expect(() => connectionRegistryHostRequestSchema.parse(request)).toThrow()
  expect(() => requestConnectionRegistry(profileId.toUpperCase())).toThrow()
})

test('refuses a schema-valid request that exceeds the bounded native envelope', () => {
  const host = installHost()
  const path = `/${'a'.repeat(4095)}`
  const profiles = Array.from({ length: MAX_CONNECTION_PROFILES }, (_, index) => ({
    profile_id: `aaaaaaaa-aaaa-4aaa-8aaa-${(index + 1).toString(16).padStart(12, '0')}`,
    label: `Remote ${index}`,
    kind: 'ssh' as const,
    enabled: index === 0,
    live_updates: true,
    expected_workspace_id: `11111111-1111-4111-8111-${(index + 1).toString(16).padStart(12, '0')}`,
    ssh_host_alias: `host${index}`,
    remote_app_dir: path,
    remote_data_dir: path,
    preferred_forward_port: 20_000 + index,
    remote_port: 8_765,
  }))
  const oversized = { schema_version: 1 as const, active_profile_id: profiles[0].profile_id, profiles }

  expect(() => saveConnectionRegistry(oversized, registryDigest, profileId)).toThrow(RangeError)
  expect(host.postMessage).not.toHaveBeenCalled()
})

test('represents CAS-aware registry replies, conflicts, and restart-only activation receipts', () => {
  const envelope = {
    type: 'workstack-connection-registry-response', schema_version: 1,
    request_id: profileId, ok: true,
  }
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope, operation: 'get-registry', result: { registry, registry_digest: registryDigest },
  }).success).toBe(true)
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope, operation: 'get-registry', result: { registry },
  }).success).toBe(false)
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope, operation: 'get-registry', result: { registry, registry_digest: null },
  }).success).toBe(false)
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope,
    operation: 'activate-profile',
    result: {
      registry,
      registry_digest: registryDigest,
      restart_required: true,
    },
  }).success).toBe(true)
  expect(connectionRegistryHostMessageSchema.safeParse({
    type: 'workstack-connection-registry-response', schema_version: 1,
    request_id: profileId, operation: 'save-registry', ok: false,
    error: { code: 'registry_conflict', message: 'Registry changed' },
  }).success).toBe(true)
  expect(connectionRegistryHostMessageSchema.safeParse({
    type: 'workstack-connection-registry-response', schema_version: 1,
    request_id: profileId, operation: 'save-registry', ok: false,
    error: { code: 'registry_conflict', message: 'Registry changed', current_registry_digest: registryDigest },
  }).success).toBe(false)
})

test('delivers only strict bounded native messages', () => {
  const host = installHost()
  const listener = vi.fn()
  const unsubscribe = subscribeConnectionRegistryHostMessages(listener)
  const valid = {
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: profileId,
    operation: 'get-registry',
    ok: true,
    result: { registry, registry_digest: registryDigest },
  }
  host.receive({ ...valid, log_path: 'C:\\secret.log' })
  host.receive({ ...valid, schema_version: 2 })
  host.receive(valid)
  host.receive({
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: profileId,
    operation: 'discover-ssh-aliases',
    ok: true,
    result: { aliases: ['work-linux'] },
  })
  host.receive({
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: profileId,
    operation: 'save-registry',
    ok: false,
    error: { code: 'invalid.registry', message: 'The registry was rejected' },
  })
  host.receive({ padding: 'x'.repeat(MAX_CONNECTION_REGISTRY_RESPONSE_BYTES) })

  expect(listener).toHaveBeenCalledTimes(3)
  unsubscribe()
  expect(host.removeEventListener).toHaveBeenCalledTimes(1)
})

test('rejects unsafe or incoherent discovered alias results', () => {
  const base = {
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: profileId,
    operation: 'discover-ssh-aliases',
    ok: true,
    result: { aliases: ['work-linux'] },
  }
  expect(connectionRegistryHostMessageSchema.safeParse({ ...base, result: { aliases: ['work-linux', 'WORK-LINUX'] } }).success).toBe(false)
  expect(connectionRegistryHostMessageSchema.safeParse({ ...base, result: { aliases: ['-oProxyCommand=calc'] } }).success).toBe(false)
  expect(connectionRegistryHostMessageSchema.safeParse({ ...base, result: { ...base.result, resolved: [{ alias: 'work-linux' }] } }).success).toBe(false)
  expect(connectionRegistryHostMessageSchema.safeParse({
    type: 'workstack-connection-registry-response',
    schema_version: 1,
    request_id: profileId,
    operation: null,
    ok: false,
    error: { code: 'invalid_request', message: 'Invalid' },
  }).success).toBe(false)
})

test('accepts only sanitized correlated browse and profile-test results', () => {
  const envelope = { type: 'workstack-connection-registry-response', schema_version: 1, request_id: profileId, ok: true }
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope, operation: 'choose-local-directory', result: { selection: 'C:/WorkStack/ssot' },
  }).success).toBe(true)
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope, operation: 'choose-local-directory', result: { selection: '\\\\server\\share' },
  }).success).toBe(false)
  const testResult = {
    profile_id: profileId, kind: 'ssh', status: 'ready', actual_workspace_id: workspaceId,
    product_version: '1.0.6', protocol_version: 1, proof_id: proofId,
  }
  expect(connectionRegistryHostMessageSchema.safeParse({ ...envelope, operation: 'test-profile', result: testResult }).success).toBe(true)
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope,
    operation: 'test-profile',
    result: {
      ...testResult, status: 'candidate', actual_workspace_id: null,
      product_version: null, protocol_version: null, proof_id: null,
    },
  }).success).toBe(true)
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope, operation: 'test-profile', result: { ...testResult, status: 'candidate' },
  }).success).toBe(false)
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope, operation: 'test-profile', result: { ...testResult, product_version: null },
  }).success).toBe(false)
  expect(connectionRegistryHostMessageSchema.safeParse({
    ...envelope, operation: 'test-profile', result: { ...testResult, command: 'ssh work-linux' },
  }).success).toBe(false)
})
