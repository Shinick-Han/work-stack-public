import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { UpdateStatusControl } from './UpdateStatusControl'

test('requests native status and installs one verified ready update', async () => {
  let listener: ((event: Event & { data?: unknown }) => void) | undefined
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(),
    postMessage,
  } } })

  render(<UpdateStatusControl />)
  expect(postMessage).toHaveBeenCalledWith('workstack-update-host|status')

  act(() => listener?.({ data: {
    type: 'workstack-update-status',
    state: 'ready',
    current_version: '1.0.4',
    latest_version: '1.0.5',
    release_url: 'https://github.com/Shinick-Han/work-stack-public/releases/tag/v1.0.5',
    message: 'Verified update will install when Work Stack closes',
    preferences: { auto_check: true, auto_download: true, install_on_exit: true },
  } } as Event & { data: unknown }))

  await userEvent.click(screen.getByRole('button', { name: 'Update 1.0.5 ready' }))
  expect(screen.getByRole('dialog', { name: 'Work Stack updates' })).toHaveTextContent('1.0.4')
  await userEvent.click(screen.getByRole('button', { name: 'Install and restart' }))
  expect(postMessage).toHaveBeenCalledWith('workstack-update-host|install')
})

test('persists explicit automatic-update preference changes through the native host', async () => {
  let listener: ((event: Event & { data?: unknown }) => void) | undefined
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    addEventListener: (_type: string, next: typeof listener) => { listener = next },
    removeEventListener: vi.fn(),
    postMessage,
  } } })
  render(<UpdateStatusControl />)
  act(() => listener?.({ data: {
    type: 'workstack-update-status', state: 'current', current_version: '1.0.5', latest_version: '1.0.5',
    release_url: '', message: 'Work Stack is up to date',
    preferences: { auto_check: true, auto_download: true, install_on_exit: true },
  } } as Event & { data: unknown }))

  await userEvent.click(screen.getByRole('button', { name: /Work Stack updates/ }))
  await userEvent.click(screen.getByRole('checkbox', { name: /Download automatically/ }))
  expect(postMessage).toHaveBeenCalledWith('workstack-update-host|preferences|1|0|1')
})
