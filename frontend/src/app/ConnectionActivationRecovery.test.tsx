import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Root } from 'react-dom/client'
import { afterEach, expect, test, vi } from 'vitest'

import {
  ConnectionActivationRecovery,
  mountConnectionActivationRecovery,
} from './ConnectionActivationRecovery'
import {
  connectionActivationRecoveryRequestSchema,
  connectionActivationRecoveryStateSchema,
  type ConnectionActivationRecoveryRequest,
} from './connectionActivationRecoveryContract'

const state = {
  type: 'workstack-connection-activation-recovery-state' as const,
  schema_version: 1 as const,
  activation_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  expected_registry_digest: `sha256:${'a'.repeat(64)}`,
  failed_profile: {
    profile_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    label: 'Work Linux',
    kind: 'ssh' as const,
  },
  previous_profile: {
    profile_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
    label: 'Local planning',
    kind: 'local' as const,
  },
  error: { code: 'activation_failed', summary: 'The selected connection did not become ready.' },
}

const mountedRoots: Root[] = []
afterEach(() => {
  act(() => mountedRoots.splice(0).forEach((root) => root.unmount()))
})

test('accepts only bounded profile-safe recovery metadata', () => {
  expect(connectionActivationRecoveryStateSchema.safeParse(state).success).toBe(true)
  expect(connectionActivationRecoveryStateSchema.safeParse({
    ...state, failed_profile: { ...state.failed_profile, remote_data_dir: '/srv/private' },
  }).success).toBe(false)
  expect(connectionActivationRecoveryStateSchema.safeParse({
    ...state, error: { ...state.error, summary: 'Failed at C:\\Users\\person\\secret' },
  }).success).toBe(false)
  expect(connectionActivationRecoveryStateSchema.safeParse({
    ...state, error: { ...state.error, summary: 'token=do-not-render' },
  }).success).toBe(false)
  expect(connectionActivationRecoveryStateSchema.safeParse({
    ...state, error: { ...state.error, summary: 'Try `ssh work-linux`' },
  }).success).toBe(false)
})

test('stays unmounted unless explicitly enabled and never submits during render', () => {
  const onExit = vi.fn()
  const onRestore = vi.fn()
  const { rerender } = render(<ConnectionActivationRecovery enabled={false} onExit={onExit} onRestore={onRestore} state={state} />)
  expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  expect(onExit).not.toHaveBeenCalled()
  expect(onRestore).not.toHaveBeenCalled()

  rerender(<ConnectionActivationRecovery enabled onExit={onExit} onRestore={onRestore} state={state} />)
  expect(screen.getByRole('alertdialog', { name: /could not activate/i })).toBeVisible()
  expect(onExit).not.toHaveBeenCalled()
  expect(onRestore).not.toHaveBeenCalled()
})

test('provides an explicit keyboard flow for restore and exit without revealing authority data', async () => {
  const onExit = vi.fn()
  const onRestore = vi.fn()
  render(<ConnectionActivationRecovery enabled onExit={onExit} onRestore={onRestore} state={state} />)

  expect(screen.getByRole('heading', { name: /could not activate/i })).toHaveFocus()
  expect(screen.getByRole('alert')).toHaveTextContent('The selected connection did not become ready.')
  expect(screen.queryByText(/srv|Users|ssh work-linux|token=/i)).not.toBeInTheDocument()
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Restore previous connection' })).toHaveFocus()
  await userEvent.keyboard('{Enter}')
  expect(onRestore).toHaveBeenCalledOnce()
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Exit' })).toHaveFocus()
  await userEvent.keyboard(' ')
  expect(onExit).toHaveBeenCalledOnce()
})

test('mount helper emits one strict CAS-bound request only after the visible action', async () => {
  const container = document.createElement('div')
  document.body.append(container)
  let resolveAction!: () => void
  const onAction = vi.fn((_request: ConnectionActivationRecoveryRequest) => new Promise<void>((resolve) => { resolveAction = resolve }))
  let root!: Root
  act(() => { root = mountConnectionActivationRecovery(container, { enabled: true, onAction, state }) })
  mountedRoots.push(root)
  const surface = within(container)

  expect(onAction).not.toHaveBeenCalled()
  await userEvent.click(surface.getByRole('button', { name: 'Restore previous connection' }))
  await userEvent.click(surface.getByRole('button', { name: 'Restoring…' }))
  expect(onAction).toHaveBeenCalledOnce()
  const request = onAction.mock.calls[0][0]
  expect(connectionActivationRecoveryRequestSchema.parse(request)).toEqual(expect.objectContaining({
    operation: 'restore-previous-connection',
    activation_id: state.activation_id,
    expected_registry_digest: state.expected_registry_digest,
  }))
  expect(JSON.stringify(request)).not.toMatch(/Work Linux|Local planning|path|command|credential/i)
  await act(async () => { resolveAction(); await Promise.resolve() })
  container.remove()
})

test('keeps raw host failure details out of the retry presentation', async () => {
  const container = document.createElement('div')
  document.body.append(container)
  const onAction = vi.fn(async () => { throw new Error('C:\\secret\\runtime.log token=private') })
  let root!: Root
  act(() => { root = mountConnectionActivationRecovery(container, { enabled: true, onAction, state }) })
  mountedRoots.push(root)

  await userEvent.click(within(container).getByRole('button', { name: 'Exit' }))
  expect(await within(container).findByText(/request was not accepted/i)).toBeVisible()
  expect(container).not.toHaveTextContent(/runtime\.log|token=private/i)
  container.remove()
})

test('keeps restore visible but unavailable when no previous profile exists', () => {
  render(<ConnectionActivationRecovery enabled onExit={vi.fn()} onRestore={vi.fn()} state={{ ...state, previous_profile: null }} />)
  expect(screen.getByRole('button', { name: 'Restore previous connection' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Exit' })).toBeEnabled()
  expect(screen.getByText('None is available to restore.')).toBeVisible()
})
