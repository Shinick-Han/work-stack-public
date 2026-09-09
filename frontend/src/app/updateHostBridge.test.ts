import { expect, test, vi } from 'vitest'
import {
  formatObservedRemoteVersion,
  hasRemoteUpdateCapability,
  hasUpdateHost,
  isOptionalObservedRemoteVersion,
  isOptionalRemoteUpdateAvailable,
  pcStatusCopy,
  remoteCapabilityKnown,
  requestRemoteOpen,
  requestUpdateCheck,
  requestUpdateDownload,
  requestUpdateStatus,
  subscribeUpdateStatus,
  type UpdateHostStatus,
} from './updateHostBridge'

const required: UpdateHostStatus = {
  type: 'workstack-update-status',
  state: 'current',
  current_version: '1.0.4',
  latest_version: '1.0.4',
  release_url: '',
  message: 'Work Stack is up to date',
  preferences: { auto_check: true, auto_download: true, install_on_exit: true },
}

function receiver() {
  let listener: ((event: Event & { data?: unknown }) => void) | undefined
  const receive = vi.fn()
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(),
    postMessage,
  } } })
  subscribeUpdateStatus(receive)
  return {
    postMessage,
    receive,
    send(data: unknown) { listener?.({ data } as Event & { data: unknown }) },
  }
}

test('old status without remote fields remains a valid host envelope', () => {
  const host = receiver()
  expect(hasUpdateHost()).toBe(true)
  host.send(required)
  expect(host.receive).toHaveBeenCalledTimes(1)
  expect(host.receive).toHaveBeenCalledWith(required)
  expect(remoteCapabilityKnown(required)).toBe(false)
  expect(hasRemoteUpdateCapability(required)).toBe(false)
})

test('admits optional remote capability and a directly observed remote version', () => {
  const host = receiver()
  const status = {
    ...required,
    remote_update_available: true,
    remote_version: '1.0.13',
  }
  host.send(status)
  expect(host.receive).toHaveBeenCalledWith(status)
  expect(formatObservedRemoteVersion(status)).toBe('1.0.13')
})

test('treats a known capability with a null or blank remote version as UNKNOWN', () => {
  const withNull = { ...required, remote_update_available: false, remote_version: null }
  const withBlank = { ...required, remote_update_available: true, remote_version: '  ' }
  expect(formatObservedRemoteVersion(withNull)).toBe('UNKNOWN')
  expect(formatObservedRemoteVersion(withBlank)).toBe('UNKNOWN')
  expect(pcStatusCopy(withNull)).toBe(
    'This PC is current. That does not mean the Linux server is current.',
  )
})

test('rejects invalid optional remote field shapes without delivering status', () => {
  const host = receiver()
  const invalid = [
    { ...required, remote_update_available: 'true' },
    { ...required, remote_update_available: 1 },
    { ...required, remote_update_available: {} },
    { ...required, remote_version: 13 },
    { ...required, remote_version: true },
    { ...required, remote_version: { value: '1.0.13' } },
    { ...required, remote_update_available: true, remote_version: ['1.0.13'] },
  ]
  for (const payload of invalid) host.send(payload)
  expect(host.receive).not.toHaveBeenCalled()
  expect(isOptionalRemoteUpdateAvailable('true')).toBe(false)
  expect(isOptionalObservedRemoteVersion(13)).toBe(false)
})

test('requestRemoteOpen posts workstack-update-host|remote-open once per call', () => {
  const host = receiver()
  requestUpdateStatus()
  requestRemoteOpen()
  requestUpdateCheck()
  requestUpdateDownload()
  expect(host.postMessage).toHaveBeenNthCalledWith(1, 'workstack-update-host|status')
  expect(host.postMessage).toHaveBeenNthCalledWith(2, 'workstack-update-host|remote-open')
  expect(host.postMessage).toHaveBeenNthCalledWith(3, 'workstack-update-host|check')
  expect(host.postMessage).toHaveBeenNthCalledWith(4, 'workstack-update-host|download')
  expect(host.postMessage.mock.calls.filter(([message]) => message === 'workstack-update-host|remote-open')).toHaveLength(1)
})
