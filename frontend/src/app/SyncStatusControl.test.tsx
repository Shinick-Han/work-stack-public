import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import type { SyncStatus } from '../domain/types'
import { ApiError } from '../api/client'
import { SyncStatusControl, SyncStatusDialog, isSyncWriteBlocked } from './SyncStatusControl'

const baseStatus: SyncStatus = {
  state: 'in-sync',
  workspace_id: 'workspace-test',
  candidate_workspace_id: 'workspace-test',
  generation: 12,
  manifest_digest: `sha256:${'a'.repeat(64)}`,
  changed_files: [],
  reason: null,
  rebind_available: false,
}

test('keeps an in-sync indicator passive', () => {
  render(<SyncStatusControl error={null} isFetching={false} onRefresh={vi.fn()} onReview={vi.fn()} status={baseStatus} />)
  expect(screen.getByRole('button', { name: 'SSOT in sync' })).toBeDisabled()
})

test('uses a configured passive status as a connection-center entry without overriding sync repair actions', async () => {
  const onConfigureSsot = vi.fn()
  const onReview = vi.fn()
  const { rerender } = render(<SyncStatusControl
    error={null} isFetching={false} onConfigureSsot={onConfigureSsot}
    onRefresh={vi.fn()} onReview={onReview} status={baseStatus}
  />)
  await userEvent.click(screen.getByRole('button', { name: /Configure SSOT connections/ }))
  expect(onConfigureSsot).toHaveBeenCalledOnce()

  rerender(<SyncStatusControl
    error={null} isFetching={false} onConfigureSsot={onConfigureSsot}
    onRefresh={vi.fn()} onReview={onReview}
    status={{ ...baseStatus, state: 'external-change-detected', changed_files: ['tasks.json'] }}
  />)
  await userEvent.click(screen.getByRole('button', { name: 'Sync / review SSOT changes' }))
  expect(onReview).toHaveBeenCalledOnce()
  expect(onConfigureSsot).toHaveBeenCalledOnce()
})

test('enables review for external changes without claiming to merge them', async () => {
  const onReview = vi.fn()
  render(<SyncStatusControl
    error={null}
    isFetching={false}
    onRefresh={vi.fn()}
    onReview={onReview}
    status={{ ...baseStatus, state: 'external-change-detected', changed_files: ['tasks.json'] }}
  />)
  await userEvent.click(screen.getByRole('button', { name: 'Sync / review SSOT changes' }))
  expect(onReview).toHaveBeenCalledOnce()
})

test('marks invalid and disconnected status as write-blocking', () => {
  const invalid = { ...baseStatus, state: 'invalid' as const }
  expect(isSyncWriteBlocked(invalid)).toBe(true)
  expect(isSyncWriteBlocked(undefined, new TypeError('first status request failed'))).toBe(true)
  expect(isSyncWriteBlocked(baseStatus, new TypeError('network down'))).toBe(true)
  expect(isSyncWriteBlocked(undefined, new ApiError(404, 'not_found', 'optional endpoint'))).toBe(false)

  render(<SyncStatusControl error={new TypeError('network down')} isFetching={false} onRefresh={vi.fn()} onReview={vi.fn()} status={baseStatus} />)
  expect(screen.getByRole('button', { name: 'Reconnect' })).toHaveTextContent('Disconnected · writes blocked')
})

test('compares the accepted generation with the detected candidate revision', () => {
  render(<SyncStatusDialog
    adoptError={null}
    adopting={false}
    onAdopt={vi.fn()}
    onClose={vi.fn()}
    onRefresh={vi.fn()}
    status={{ ...baseStatus, state: 'external-change-detected', changed_files: ['tasks.json'], reason: 'External edit detected' }}
  />)
  expect(screen.getByRole('dialog', { name: 'Review SSOT update' })).toBeInTheDocument()
  const comparison = screen.getByRole('region', { name: 'SSOT revision comparison' })
  expect(comparison).toHaveTextContent('Accepted baseline')
  expect(comparison).toHaveTextContent('Generation 12')
  expect(comparison).toHaveTextContent('Detected external candidate')
  expect(comparison).toHaveTextContent(`Revision ${'a'.repeat(12)}`)
  expect(screen.getByText('tasks.json')).toBeInTheDocument()
  expect(screen.getByText(/nothing is copied, merged, or overwritten/i)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Accept reviewed candidate' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Refresh comparison' })).toBeEnabled()
})

test.each([
  ['stale', 'Refresh stale SSOT review'],
  ['invalid', 'Resolve SSOT conflict'],
] as const)('keeps %s state read-only and requires refresh or repair', (state, title) => {
  render(<SyncStatusDialog
    adoptError={null}
    adopting={false}
    onAdopt={vi.fn()}
    onClose={vi.fn()}
    onRefresh={vi.fn()}
    status={{ ...baseStatus, state, reason: 'Candidate is not safe to accept' }}
  />)
  expect(screen.getByRole('dialog', { name: title })).toHaveTextContent('Candidate is not safe to accept')
  expect(screen.queryByRole('button', { name: 'Accept reviewed candidate' })).not.toBeInTheDocument()
  expect(screen.getByText(/writes remain paused/i)).toBeVisible()
})

test('keeps a failed adoption visibly unaccepted and points to a fresh review', () => {
  render(<SyncStatusDialog
    adoptError="sync candidate changed"
    adopting={false}
    onAdopt={vi.fn()}
    onClose={vi.fn()}
    onRefresh={vi.fn()}
    status={{ ...baseStatus, state: 'external-change-detected', changed_files: ['tasks.json'] }}
  />)
  expect(screen.getByRole('alert')).toHaveTextContent('The reviewed candidate was not accepted')
  expect(screen.getByRole('alert')).toHaveTextContent('Refresh comparison before retrying')
})

test('does not accept a candidate while its comparison is refreshing', () => {
  render(<SyncStatusDialog
    adoptError={null}
    adopting={false}
    onAdopt={vi.fn()}
    onClose={vi.fn()}
    onRefresh={vi.fn()}
    refreshing
    status={{ ...baseStatus, state: 'external-change-detected', changed_files: ['tasks.json'] }}
  />)
  expect(screen.getByRole('button', { name: 'Refreshing comparison…' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Accept reviewed candidate' })).toBeDisabled()
})

test('requires exact candidate identity before workspace rebind', async () => {
  const onRebind = vi.fn()
  const candidate = '99999999-9999-4999-8999-999999999999'
  render(<SyncStatusDialog
    adoptError={null}
    adopting={false}
    onAdopt={vi.fn()}
    onClose={vi.fn()}
    onRebind={onRebind}
    onRefresh={vi.fn()}
    rebindPreview={{
      state: 'workspace-identity-mismatch',
      manifest_workspace_id: '11111111-1111-4111-8111-111111111111',
      candidate_workspace_id: candidate,
      manifest_digest: `sha256:${'a'.repeat(64)}`,
      candidate_digest: `sha256:${'b'.repeat(64)}`,
      changed_files: ['workspace.json'],
    }}
    status={{
      ...baseStatus,
      state: 'invalid',
      candidate_workspace_id: candidate,
      rebind_available: true,
    }}
  />)
  expect(screen.getByRole('dialog', { name: 'Review different workspace identity' })).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'SSOT revision comparison' })).toHaveTextContent('Different workspace identity')
  expect(screen.getByRole('region', { name: 'SSOT revision comparison' })).toHaveTextContent('Explicit review required')
  expect(screen.getByText(/configured path now identifies a different workspace/i)).toBeVisible()
  expect(screen.queryByText('Rejected external candidate')).not.toBeInTheDocument()
  expect(screen.queryByText('Validation failed')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Accept reviewed candidate' })).not.toBeInTheDocument()
  const action = screen.getByRole('button', { name: 'Back up and reconnect workspace' })
  expect(action).toBeDisabled()
  await userEvent.type(screen.getByLabelText('Type the candidate identity to confirm'), candidate)
  expect(action).toBeEnabled()
  await userEvent.click(action)
  expect(onRebind).toHaveBeenCalledOnce()
})

test('labels a recoverable workspace identity change as an explicit review action', async () => {
  const onReview = vi.fn()
  render(<SyncStatusControl
    error={null}
    isFetching={false}
    onRefresh={vi.fn()}
    onReview={onReview}
    status={{ ...baseStatus, state: 'invalid', rebind_available: true }}
  />)

  const action = screen.getByRole('button', { name: 'Review workspace identity' })
  expect(action).toHaveTextContent('Different workspace identity · review required')
  await userEvent.click(action)
  expect(onReview).toHaveBeenCalledOnce()
})
