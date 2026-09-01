import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { SsotConnectionCenter } from './SsotConnectionCenter'

function installHost() {
  let listener: ((event: Event & { data?: unknown }) => void) | undefined
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(), postMessage,
  } } })
  return { postMessage, status(data: unknown) { act(() => listener?.({ data } as Event & { data: unknown })) } }
}

test('renders the existing passive workspace summary in an ordinary browser', () => {
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
  render(<SsotConnectionCenter fallbackDetail="No native bridge" fallbackLabel="Local workspace" />)
  expect(screen.getByText('Local workspace')).toBeVisible()
  expect(screen.getByText('No native bridge')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Configure SSOT connection' })).not.toBeInTheDocument()
})

test('shows native status, edits a remote profile, tests it, and saves it', async () => {
  const host = installHost()
  render(<SsotConnectionCenter />)
  expect(host.postMessage).toHaveBeenCalledWith('workstack-ssot-host|status')
  host.status({
    type: 'workstack-ssot-connection-status', state: 'ready', storage_mode: 'local', profile: {},
    message: 'Using the local workspace', restart_required: false, log_path: '',
  })
  await userEvent.click(screen.getByRole('button', { name: 'Configure SSOT connection' }))
  await userEvent.click(screen.getByRole('radio', { name: /Remote SSH workspace/ }))
  await userEvent.type(screen.getByLabelText(/SSH host alias/), 'work-linux')
  await userEvent.type(screen.getByLabelText('Remote app directory'), '/srv/work-stack')
  await userEvent.type(screen.getByLabelText('Remote SSOT directory'), '/srv/ssot')
  await userEvent.type(screen.getByLabelText(/Workspace ID/), '50d48f55-2cf3-4211-8a1e-5e668edaf622')
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }))
  const testMessage = host.postMessage.mock.calls.find(([message]) => String(message).startsWith('workstack-ssot-host|test|'))?.[0]
  expect(JSON.parse(decodeURIComponent(String(testMessage).split('|')[2]))).toMatchObject({
    storage_mode: 'ssh-remote', ssh_host_alias: 'work-linux', local_forward_port: 18765,
  })
  await userEvent.click(screen.getByRole('button', { name: 'Save settings' }))
  expect(host.postMessage).toHaveBeenCalledWith(expect.stringMatching(/^workstack-ssot-host\|save\|/))
})

test('blocks unsafe SSH arguments and incomplete remote drafts', async () => {
  const host = installHost()
  render(<SsotConnectionCenter />)
  await userEvent.click(screen.getByRole('button', { name: 'Configure SSOT connection' }))
  await userEvent.click(screen.getByRole('radio', { name: /Remote SSH workspace/ }))
  await userEvent.type(screen.getByLabelText(/SSH host alias/), 'host -o ProxyCommand=bad')
  expect(screen.getByRole('button', { name: 'Test connection' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Save settings' })).toBeDisabled()
  expect(host.postMessage).not.toHaveBeenCalledWith(expect.stringMatching(/^workstack-ssot-host\|(test|save)\|/))
})

test('surfaces restart-required status clearly', async () => {
  const host = installHost()
  render(<SsotConnectionCenter />)
  host.status({
    type: 'workstack-ssot-connection-status', state: 'saved', storage_mode: 'local', profile: {},
    message: 'Local workspace selected', restart_required: true, log_path: 'C:\\Logs\\work-stack.log',
  })
  await userEvent.click(screen.getByRole('button', { name: 'Configure SSOT connection' }))
  expect(screen.getByText(/Restart Work Stack to activate/)).toBeVisible()
  expect(screen.getByText(/work-stack\.log/)).toBeVisible()
})

test('shows bounded remote reconnection and terminal disconnection states', async () => {
  const host = installHost()
  render(<SsotConnectionCenter />)
  host.status({
    type: 'workstack-ssot-connection-status', state: 'reconnecting', storage_mode: 'ssh-remote',
    profile: {
      ssh_host_alias: 'work-linux', remote_app_dir: '/srv/work-stack', remote_data_dir: '/srv/ssot',
      local_forward_port: 18765, remote_port: 8765, workspace_id: '50d48f55-2cf3-4211-8a1e-5e668edaf622',
    },
    message: 'Reconnecting safely...', restart_required: false, log_path: 'C:\\Logs\\remote-ssh.log',
  })
  expect(screen.getByRole('button', { name: 'Configure SSOT connection' })).toHaveTextContent('Reconnecting')
  await userEvent.click(screen.getByRole('button', { name: 'Configure SSOT connection' }))
  expect(screen.getByRole('button', { name: 'Reconnecting…' })).toBeDisabled()

  host.status({
    type: 'workstack-ssot-connection-status', state: 'disconnected', storage_mode: 'ssh-remote',
    profile: {
      ssh_host_alias: 'work-linux', remote_app_dir: '/srv/work-stack', remote_data_dir: '/srv/ssot',
      local_forward_port: 18765, remote_port: 8765, workspace_id: '50d48f55-2cf3-4211-8a1e-5e668edaf622',
    },
    message: 'Reconnection exhausted.', restart_required: false, log_path: 'C:\\Logs\\remote-ssh.log',
  })
  expect(screen.getByRole('alert')).toHaveTextContent('Reconnection exhausted')
})

test('exposes session-only change detection, runtime port, reconnect, and diagnostics actions', async () => {
  const host = installHost()
  render(<SsotConnectionCenter />)
  host.status({
    type: 'workstack-ssot-connection-status', state: 'ready', storage_mode: 'ssh-remote',
    profile: {
      ssh_host_alias: 'work-linux', remote_app_dir: '/srv/work-stack', remote_data_dir: '/srv/ssot',
      local_forward_port: 18765, remote_port: 8765, workspace_id: '50d48f55-2cf3-4211-8a1e-5e668edaf622',
    },
    message: 'Remote SSOT connection is healthy.', restart_required: false,
    log_path: 'C:\\Logs\\remote-ssh.log', session_change_detection: true, runtime_forward_port: 24567,
  })

  await userEvent.click(screen.getByRole('button', { name: 'Configure SSOT connection' }))
  expect(screen.getByText(/Live while Work Stack is open/)).toBeVisible()
  expect(screen.getByText(/24567/)).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Reconnect now' }))
  await userEvent.click(screen.getByRole('button', { name: 'Open diagnostics folder' }))

  expect(host.postMessage).toHaveBeenCalledWith('workstack-ssot-host|reconnect')
  expect(host.postMessage).toHaveBeenCalledWith('workstack-ssot-host|open-diagnostics')
})
