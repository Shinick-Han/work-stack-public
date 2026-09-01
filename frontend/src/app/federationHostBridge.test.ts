import { afterEach, expect, test, vi } from 'vitest'

import {
  MAX_PORTFOLIO_MESSAGE_BYTES,
  hasFederationHost,
  requestFederationStatus,
  requestFederationWorkspaceSwitch,
  requestPortfolioProjection,
  subscribeFederationStatus,
  subscribePortfolioProjection,
} from './federationHostBridge'
import { portfolioProjectionSchema } from '../domain/federationSchemas'

interface WebViewMessageEvent extends Event { data?: unknown }

const profileId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const workspaceId = '11111111-1111-4111-8111-111111111111'
const observedAt = '2026-09-01T12:00:00+09:00'

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

test('emits only versioned status, read, and structured switch requests', () => {
  const host = installHost()
  expect(hasFederationHost()).toBe(true)
  expect(requestFederationStatus()).toBe(true)
  expect(requestPortfolioProjection([profileId])).toBe(true)
  expect(requestFederationWorkspaceSwitch(workspaceId, {
    workspace_id: workspaceId,
    entity_type: 'task',
    entity_id: 'T-0001',
  })).toBe(true)

  expect(host.postMessage.mock.calls.map(([value]) => JSON.parse(value))).toEqual([
    { type: 'workstack-federation-request', schema_version: '1.0', operation: 'status' },
    { type: 'workstack-federation-request', schema_version: '1.0', operation: 'read', profile_ids: [profileId] },
    {
      type: 'workstack-federation-request', schema_version: '1.0', operation: 'switch', workspace_id: workspaceId,
      target: { workspace_id: workspaceId, entity_type: 'task', entity_id: 'T-0001' },
    },
  ])
  expect(() => requestPortfolioProjection(['https://remote.invalid'])).toThrow()
  expect(() => requestFederationWorkspaceSwitch('ssh work-linux' as string)).toThrow()
  expect(() => requestPortfolioProjection(['AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'])).toThrow()
  expect(() => requestPortfolioProjection(['00000000-0000-0000-0000-000000000000'])).toThrow()
  expect(() => requestFederationWorkspaceSwitch(workspaceId, {
    workspace_id: '22222222-2222-4222-8222-222222222222',
    entity_type: 'task',
    entity_id: 'T-0001',
  })).toThrow()
})

test('delivers only strict bounded status messages', () => {
  const host = installHost()
  const receive = vi.fn()
  const unsubscribe = subscribeFederationStatus(receive)
  const valid = {
    type: 'workstack-federation-status',
    schema_version: '1.0',
    active_profile_id: profileId,
    statuses: [{
      profile_id: profileId,
      state: 'ready',
      expected_workspace_id: workspaceId,
      actual_workspace_id: workspaceId,
      product_version: '1.0.6',
      protocol_version: 1,
      session_port: 24567,
      confirmed_generation: 8,
      observed_at: observedAt,
      error_code: null,
    }],
  }
  host.receive({ ...valid, raw_ssh_command: 'ssh work-linux' })
  host.receive({ ...valid, statuses: [{ ...valid.statuses[0], session_port: 70_000 }] })
  host.receive(valid)
  expect(receive).toHaveBeenCalledTimes(1)
  expect(receive).toHaveBeenCalledWith(valid)
  unsubscribe()
  expect(host.removeEventListener).toHaveBeenCalledTimes(1)
})

test('rejects malformed Portfolio content and accepts a read-only projection', () => {
  const host = installHost()
  const receive = vi.fn()
  subscribePortfolioProjection(receive)
  const valid = {
    type: 'workstack-federation-portfolio',
    schema_version: '1.0',
    generated_at: observedAt,
    workspaces: [{
      profile_id: profileId,
      workspace_id: workspaceId,
      name: 'Engineering',
      state: 'ready',
      confirmed_generation: 8,
      observed_at: observedAt,
      stale: false,
    }],
    tasks: [],
    unavailable_sources: [],
  }
  host.receive({ ...valid, tasks: [{ title: 'missing provenance' }] })
  host.receive({ ...valid, command: { method: 'POST', url: '/api/v1/tasks' } })
  host.receive(valid)
  expect(receive).toHaveBeenCalledTimes(1)
  expect(receive).toHaveBeenCalledWith(valid)
})

test('returns false without a native host', () => {
  expect(hasFederationHost()).toBe(false)
  expect(requestFederationStatus()).toBe(false)
})

test('the maximum v1 projection shape stays within the bounded bridge envelope', () => {
  const uuid = (prefix: string, index: number) => `${prefix}0000000-0000-4000-8000-${index.toString(16).padStart(12, '0')}`
  const workspaces = Array.from({ length: 128 }, (_, index) => ({
    profile_id: uuid('a', index + 1),
    workspace_id: uuid('1', index + 1),
    name: '한'.repeat(200),
    state: 'ready',
    confirmed_generation: 8,
    observed_at: observedAt,
    stale: false,
  }))
  const tasks = Array.from({ length: 128 }, (_, index) => ({
    ref: { workspace_id: workspaces[index].workspace_id, entity_type: 'task', entity_id: `T${'x'.repeat(126)}${index % 10}` },
    title: '한'.repeat(500),
    status: 'started',
    priority: 'P1',
    due: '2026-09-05',
    objective_refs: Array.from({ length: 16 }, (_unused, objectiveIndex) => ({
      workspace_id: workspaces[index].workspace_id,
      entity_type: 'objective',
      entity_id: `O${objectiveIndex.toString(16).padStart(2, '0')}${'x'.repeat(125)}`,
    })),
    revision: 3,
    source_generation: 8,
    observed_at: observedAt,
    stale: false,
  }))
  const projection = {
    type: 'workstack-federation-portfolio',
    schema_version: '1.0',
    generated_at: observedAt,
    workspaces,
    tasks,
    unavailable_sources: Array.from({ length: 128 }, (_, index) => ({
      profile_id: uuid('b', index + 1),
      expected_workspace_id: uuid('2', index + 1),
      state: 'connecting',
      error_code: null,
    })),
  }

  expect(portfolioProjectionSchema.safeParse(projection).success).toBe(true)
  expect(new TextEncoder().encode(JSON.stringify(projection)).byteLength).toBeLessThanOrEqual(MAX_PORTFOLIO_MESSAGE_BYTES)
})
