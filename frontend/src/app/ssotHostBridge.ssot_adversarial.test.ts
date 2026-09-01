import { expect, test, vi } from 'vitest'
import { subscribeSsotConnectionStatus } from './ssotHostBridge'

interface WebViewMessageEvent extends Event { data?: unknown }

function receiver() {
  let listener: ((event: WebViewMessageEvent) => void) | undefined
  const receive = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(),
    postMessage: vi.fn(),
  } } })
  subscribeSsotConnectionStatus(receive)
  return {
    receive,
    send(data: unknown) { listener?.({ data } as WebViewMessageEvent) },
  }
}

const validRemote = {
  type: 'workstack-ssot-connection-status',
  state: 'ready',
  storage_mode: 'ssh-remote',
  profile: {
    ssh_host_alias: 'work-linux',
    remote_app_dir: '/srv/work-stack',
    remote_data_dir: '/srv/ssot',
    local_forward_port: 18765,
    remote_port: 8765,
    workspace_id: '50d48f55-2cf3-4211-8a1e-5e668edaf622',
  },
  message: 'Connected',
  restart_required: false,
  log_path: '',
}

test('rejects malformed native SSOT status envelopes', () => {
  const host = receiver()
  const malformed = [
    null,
    [],
    { ...validRemote, state: 'connected' },
    { ...validRemote, storage_mode: 'network-share' },
    { ...validRemote, message: 42 },
    { ...validRemote, restart_required: 'false' },
    { ...validRemote, log_path: null },
    { ...validRemote, profile: [] },
    { ...validRemote, profile: { ...validRemote.profile, remote_port: '8765' } },
  ]

  for (const status of malformed) host.send(status)
  expect(host.receive).not.toHaveBeenCalled()
})

test('rejects local status carrying a remote or secret-bearing profile', () => {
  const host = receiver()
  host.send({ ...validRemote, storage_mode: 'local', profile: validRemote.profile })
  host.send({
    ...validRemote,
    storage_mode: 'local',
    profile: { password: ['not', 'allowed'].join('-') },
  })
  expect(host.receive).not.toHaveBeenCalled()
})

test('delivers one structurally valid native status after rejecting malformed messages', () => {
  const host = receiver()
  host.send({ ...validRemote, profile: { ...validRemote.profile, local_forward_port: false } })
  host.send(validRemote)
  expect(host.receive).toHaveBeenCalledTimes(1)
  expect(host.receive).toHaveBeenCalledWith(validRemote)
})
