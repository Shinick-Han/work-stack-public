import { expect, test, vi } from 'vitest'
import {
  hasSsotHost,
  requestSsotDiagnostics,
  requestSsotReconnect,
  notifySsotWorkspaceRebound,
  coordinateSsotWorkspaceRebind,
  requestSsotConnectionStatus,
  requestSsotConnectionTest,
  saveSsotConnection,
  subscribeSsotConnectionStatus,
  type SsotConnectionDraft,
} from './ssotHostBridge'

const remoteDraft: SsotConnectionDraft = {
  storage_mode: 'ssh-remote',
  ssh_host_alias: 'work-linux', remote_app_dir: '/srv/work-stack', remote_data_dir: '/srv/ssot',
  local_forward_port: 18765, remote_port: 8765, workspace_id: '50d48f55-2cf3-4211-8a1e-5e668edaf622',
}

test('detects the native host and encodes bounded status, test, and save commands', async () => {
  let listener: ((event: Event & { data?: unknown }) => void) | undefined
  const workspaceId = '22222222-2222-4222-8222-222222222222'
  const postMessage = vi.fn((message: string) => {
    if (message.includes('rebind-start')) listener?.({
      data: { type: 'workstack-ssot-rebind-ready', workspace_id: workspaceId },
    } as Event & { data: unknown })
  })
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(), postMessage,
  } } })
  expect(hasSsotHost()).toBe(true)
  requestSsotConnectionStatus()
  requestSsotConnectionTest(remoteDraft)
  saveSsotConnection({ storage_mode: 'local' })
  requestSsotReconnect()
  requestSsotDiagnostics()
  await coordinateSsotWorkspaceRebind(workspaceId)
  notifySsotWorkspaceRebound(workspaceId)
  expect(postMessage).toHaveBeenNthCalledWith(1, 'workstack-ssot-host|status')
  expect(postMessage).toHaveBeenNthCalledWith(2, `workstack-ssot-host|test|${encodeURIComponent(JSON.stringify(remoteDraft))}`)
  expect(postMessage).toHaveBeenNthCalledWith(3, `workstack-ssot-host|save|${encodeURIComponent(JSON.stringify({ storage_mode: 'local' }))}`)
  expect(postMessage).toHaveBeenNthCalledWith(4, 'workstack-ssot-host|reconnect')
  expect(postMessage).toHaveBeenNthCalledWith(5, 'workstack-ssot-host|open-diagnostics')
  expect(postMessage).toHaveBeenNthCalledWith(6, 'workstack-ssot-host|rebind-start|22222222-2222-4222-8222-222222222222')
  expect(postMessage).toHaveBeenNthCalledWith(7, 'workstack-ssot-host|rebind-complete|22222222-2222-4222-8222-222222222222')
})

test('delivers only valid native SSOT status messages', () => {
  let listener: ((event: Event & { data?: unknown }) => void) | undefined
  const receive = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(), postMessage: vi.fn(),
  } } })
  subscribeSsotConnectionStatus(receive)
  listener?.({ data: { type: 'not-ssot' } } as Event & { data: unknown })
  listener?.({ data: {
    type: 'workstack-ssot-connection-status', state: 'ready', storage_mode: 'ssh-remote',
    profile: {
      ssh_host_alias: 'work-linux', remote_app_dir: '/srv/work-stack', remote_data_dir: '/srv/ssot',
      local_forward_port: 18765, remote_port: 8765, workspace_id: '50d48f55-2cf3-4211-8a1e-5e668edaf622',
    },
    message: 'Connected', restart_required: false, log_path: 'C:\\Logs\\work-stack.log',
    session_change_detection: true, runtime_forward_port: 24567,
  } } as Event & { data: unknown })
  expect(receive).toHaveBeenCalledTimes(1)
  expect(receive).toHaveBeenCalledWith(expect.objectContaining({ state: 'ready', storage_mode: 'ssh-remote' }))
})

test('rejects malformed remote runtime diagnostics', () => {
  let listener: ((event: Event & { data?: unknown }) => void) | undefined
  const receive = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(), postMessage: vi.fn(),
  } } })
  subscribeSsotConnectionStatus(receive)
  listener?.({ data: {
    type: 'workstack-ssot-connection-status', state: 'ready', storage_mode: 'ssh-remote',
    profile: remoteDraft, message: 'Connected', restart_required: false, log_path: '',
    session_change_detection: true, runtime_forward_port: 70000,
  } } as Event & { data: unknown })
  expect(receive).not.toHaveBeenCalled()
})
