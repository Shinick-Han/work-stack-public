import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { UpdateStatusControl, updateStatusPresentation } from './UpdateStatusControl'
import type { UpdateHostStatus, UpdateState } from './updateHostBridge'

function status(state: UpdateState, overrides: Partial<UpdateHostStatus> = {}): UpdateHostStatus {
  return {
    type: 'workstack-update-status',
    state,
    current_version: '1.0.4',
    latest_version: '1.0.5',
    release_url: '',
    message: 'Status message',
    preferences: { auto_check: true, auto_download: true, install_on_exit: true },
    ...overrides,
  }
}

function mountHost() {
  let listener: ((event: Event & { data?: unknown }) => void) | undefined
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(),
    postMessage,
  } } })
  return {
    postMessage,
    publish(data: unknown) {
      act(() => listener?.({ data } as Event & { data: unknown }))
    },
  }
}

test.each([
  ['ready', 'Update 1.0.5 ready', false, true],
  ['available', 'Update 1.0.5 available', false, true],
  ['downloading', 'Downloading 1.0.5', true, false],
  ['error', 'Update check failed', false, true],
  ['blocked', 'Work Stack updates · 1.0.4', false, true],
  ['checking', 'Work Stack updates · 1.0.4', true, false],
  ['installing', 'Work Stack updates · 1.0.4', true, false],
  ['current', 'Work Stack updates · 1.0.4', false, false],
] as const)('projects %s update presentation', (state, label, busy, attention) => {
  expect(updateStatusPresentation(status(state))).toEqual({ attention, busy, label })
})

test('requests native status and installs one verified ready update', async () => {
  const host = mountHost()
  render(<UpdateStatusControl />)
  expect(host.postMessage).toHaveBeenCalledWith('workstack-update-host|status')

  host.publish({
    type: 'workstack-update-status',
    state: 'ready',
    current_version: '1.0.4',
    latest_version: '1.0.5',
    release_url: 'https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.5',
    message: 'Verified update will install when Work Stack closes',
    preferences: { auto_check: true, auto_download: true, install_on_exit: true },
  })

  await userEvent.click(screen.getByRole('button', { name: 'Update 1.0.5 ready' }))
  const dialog = screen.getByRole('dialog', { name: 'Work Stack updates' })
  expect(dialog).toHaveTextContent('1.0.4')
  expect(dialog).toHaveTextContent('Desktop version')
  expect(dialog).not.toHaveTextContent('Remote version')
  expect(screen.queryByRole('button', { name: 'Update connected Linux server' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Install and restart' }))
  expect(host.postMessage).toHaveBeenCalledWith('workstack-update-host|install')
})

test('old status compatibility hides the remote entry instead of a dead button', async () => {
  const host = mountHost()
  render(<UpdateStatusControl />)
  host.publish({
    type: 'workstack-update-status',
    state: 'current',
    current_version: '1.0.5',
    latest_version: '1.0.5',
    release_url: '',
    message: 'Work Stack is up to date',
    preferences: { auto_check: true, auto_download: true, install_on_exit: true },
  })

  await userEvent.click(screen.getByRole('button', { name: /Work Stack updates/ }))
  expect(screen.getByRole('dialog', { name: 'Work Stack updates' })).toHaveTextContent('Work Stack is up to date')
  expect(screen.queryByText('UNKNOWN')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Update connected Linux server' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Check now' }))
  expect(host.postMessage).toHaveBeenCalledWith('workstack-update-host|check')
})

test('invalid optional remote shapes do not replace a previously delivered status', async () => {
  const host = mountHost()
  render(<UpdateStatusControl />)
  host.publish(status('available'))
  await userEvent.click(screen.getByRole('button', { name: 'Update 1.0.5 available' }))
  expect(screen.getByRole('button', { name: 'Download update' })).toBeInTheDocument()

  host.publish({ ...status('current'), remote_update_available: 'yes', remote_version: 12 })
  expect(screen.getByRole('button', { name: 'Download update' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Update connected Linux server' })).not.toBeInTheDocument()
  expect(screen.queryByText('UNKNOWN')).not.toBeInTheDocument()
})

test('opens the native remote page with workstack-update-host|remote-open once per click', async () => {
  const host = mountHost()
  render(<UpdateStatusControl />)
  host.publish(status('current', {
    current_version: '1.0.5',
    latest_version: '1.0.5',
    remote_update_available: true,
    remote_version: '1.0.13',
  }))

  await userEvent.click(screen.getByRole('button', { name: /Work Stack updates/ }))
  expect(host.postMessage.mock.calls.filter(([message]) => message === 'workstack-update-host|remote-open')).toHaveLength(0)
  await userEvent.click(screen.getByRole('button', { name: 'Update connected Linux server' }))
  expect(host.postMessage.mock.calls.filter(([message]) => message === 'workstack-update-host|remote-open')).toEqual([
    ['workstack-update-host|remote-open'],
  ])
})

test('a remote version mismatch does not claim the Linux server is current and still checks this PC', async () => {
  const host = mountHost()
  render(<UpdateStatusControl />)
  host.publish(status('current', {
    current_version: '1.0.5',
    latest_version: '1.0.5',
    message: 'Work Stack is up to date',
    remote_update_available: true,
    remote_version: '1.0.12',
  }))

  await userEvent.click(screen.getByRole('button', { name: /Work Stack updates/ }))
  const dialog = screen.getByRole('dialog', { name: 'Work Stack updates' })
  expect(dialog).toHaveTextContent('Desktop version')
  expect(dialog).toHaveTextContent('Remote version')
  expect(dialog).toHaveTextContent('1.0.12')
  expect(dialog).toHaveTextContent('This PC is current. That does not mean the Linux server is current.')
  expect(dialog).toHaveTextContent('The PC installer does not update the connected Linux server.')
  expect(dialog).not.toHaveTextContent('Work Stack is up to date')
  await userEvent.click(screen.getByRole('button', { name: 'Check now' }))
  expect(host.postMessage).toHaveBeenCalledWith('workstack-update-host|check')
})

test('offline remote capability shows UNKNOWN without a dead button and still downloads this PC update', async () => {
  const host = mountHost()
  render(<UpdateStatusControl />)
  host.publish(status('available', {
    remote_update_available: false,
    remote_version: null,
  }))

  await userEvent.click(screen.getByRole('button', { name: 'Update 1.0.5 available' }))
  const dialog = screen.getByRole('dialog', { name: 'Work Stack updates' })
  expect(dialog).toHaveTextContent('Remote version')
  expect(dialog).toHaveTextContent('UNKNOWN')
  expect(screen.queryByRole('button', { name: 'Update connected Linux server' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Download update' }))
  expect(host.postMessage).toHaveBeenCalledWith('workstack-update-host|download')
})

test('persists explicit automatic-update preference changes through the native host', async () => {
  const host = mountHost()
  render(<UpdateStatusControl />)
  host.publish({
    type: 'workstack-update-status', state: 'current', current_version: '1.0.5', latest_version: '1.0.5',
    release_url: '', message: 'Work Stack is up to date',
    preferences: { auto_check: true, auto_download: true, install_on_exit: true },
  })

  await userEvent.click(screen.getByRole('button', { name: /Work Stack updates/ }))
  await userEvent.click(screen.getByRole('checkbox', { name: /Download automatically/ }))
  expect(host.postMessage).toHaveBeenCalledWith('workstack-update-host|preferences|1|0|1')
})
