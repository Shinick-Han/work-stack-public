import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import type { SyncStatus } from '../domain/types'
import { ApiError } from '../api/client'
import { SyncStatusControl, SyncStatusDialog, isSyncWriteBlocked } from './SyncStatusControl'

const baseStatus: SyncStatus = {
  state: 'in-sync',
  workspace_id: 'workspace-test',
  generation: 12,
  manifest_digest: `sha256:${'a'.repeat(64)}`,
  changed_files: [],
  reason: null,
}

test('keeps an in-sync indicator passive', () => {
  render(<SyncStatusControl error={null} isFetching={false} onRefresh={vi.fn()} onReview={vi.fn()} status={baseStatus} />)
  expect(screen.getByRole('button', { name: 'SSOT in sync' })).toBeDisabled()
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
  await userEvent.click(screen.getByRole('button', { name: 'Review SSOT changes' }))
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

test('shows a bounded read-only change summary', () => {
  render(<SyncStatusDialog
    adoptError={null}
    adopting={false}
    onAdopt={vi.fn()}
    onClose={vi.fn()}
    onRefresh={vi.fn()}
    status={{ ...baseStatus, state: 'external-change-detected', changed_files: ['tasks.json'], reason: 'External edit detected' }}
  />)
  expect(screen.getByRole('dialog', { name: 'Review SSOT changes' })).toBeInTheDocument()
  expect(screen.getByText('tasks.json')).toBeInTheDocument()
  expect(screen.getByText(/does not merge or overwrite/i)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Accept validated SSOT changes' })).toBeEnabled()
})
