import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, test, vi } from 'vitest'

import { CheckpointHistory, type FrozenAttempt } from './CheckpointHistory'
import type { CheckpointAudit, CheckpointAuditEntry } from '../../domain/types'

const WORKSPACE = 'f67e2aad-9ed9-4fc7-b1ae-63b240269855'
const CP_A = `CP-${'a'.repeat(64)}`
const CP_B = `CP-${'b'.repeat(64)}`
const DIGEST = `sha256:${'c'.repeat(64)}`

function entry(overrides: Partial<CheckpointAuditEntry> = {}): CheckpointAuditEntry {
  return {
    locator: {
      workspace_uid: WORKSPACE,
      task_id: 'T-0001',
      date: '2026-09-03',
      ordinal: 0,
      entry_digest: DIGEST,
    },
    checkpoint_id: CP_A,
    entry: { done: ['shipped'] },
    recorded: {
      type: 'worklog.recorded',
      workspace_uid: WORKSPACE,
      task_id: 'T-0001',
      checkpoint_id: CP_A,
      date: '2026-09-03',
      ordinal: 0,
      entry_digest: DIGEST,
      origin: 'agent-cli-v1',
    },
    state: 'active',
    revision: 0,
    transitions: [],
    ...overrides,
  }
}

function audit(entries: CheckpointAuditEntry[]): CheckpointAudit {
  return { workspace_uid: WORKSPACE, entries }
}

function renderHistory(
  entries: CheckpointAuditEntry[],
  extra: Partial<React.ComponentProps<typeof CheckpointHistory>> = {},
) {
  const onSubmit = vi.fn()
  let counter = 0
  render(
    <CheckpointHistory
      audit={audit(entries)}
      date="2026-09-03"
      onSubmit={onSubmit}
      owner="owner-1"
      createIdempotencyKey={() => `key-${++counter}`}
      {...extra}
    />,
  )
  return { onSubmit }
}

describe('history display', () => {
  test('filters to the selected day only after the whole audit arrives', () => {
    renderHistory([
      entry(),
      entry({ checkpoint_id: CP_B, locator: { ...entry().locator, date: '2026-09-02', ordinal: 1 } }),
    ])
    expect(screen.getByText(CP_A)).toBeVisible()
    expect(screen.queryByText(CP_B)).toBeNull()
  })

  test('shows every transition with its reason and explanation', () => {
    renderHistory([
      entry({
        state: 'superseded',
        revision: 1,
        transitions: [
          {
            type: 'worklog.superseded',
            workspace_uid: WORKSPACE,
            task_id: 'T-0001',
            checkpoint_id: CP_A,
            date: '2026-09-03',
            ordinal: 0,
            entry_digest: DIGEST,
            state: 'superseded',
            revision: 1,
            reason: { code: 'incorrect', explanation: 'Wrong day' },
            origin: 'agent-cli-v1',
          },
        ],
      }),
    ])
    const list = screen.getByRole('list', { name: `Transitions for ${CP_A}` })
    expect(within(list).getByText(/incorrect/)).toBeVisible()
    expect(within(list).getByText(/Wrong day/)).toBeVisible()
  })

  test('renders opaque entry data with a safe fallback', () => {
    const cyclic: Record<string, unknown> = {}
    cyclic.self = cyclic
    renderHistory([entry({ entry: cyclic })])
    expect(screen.getByText('Entry content is not displayable')).toBeVisible()
  })

  test('a legacy null-checkpoint row has no mutation control', () => {
    renderHistory([entry({ checkpoint_id: null, recorded: null })])
    expect(screen.getByText('Legacy entry')).toBeVisible()
    expect(screen.queryByRole('button', { name: /Supersede/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Restore/ })).toBeNull()
  })
})

describe('confirmation freezes one attempt', () => {
  test('captures checkpoint, revision, verbatim explanation and one key', async () => {
    const user = userEvent.setup()
    const { onSubmit } = renderHistory([entry({ revision: 2, state: 'active' })])

    await user.click(screen.getByRole('button', { name: `Supersede ${CP_A}` }))
    await user.selectOptions(screen.getByLabelText('Supersede reason code'), 'duplicate')
    // Surrounding whitespace must survive: Python normalization is authoritative.
    await user.type(screen.getByLabelText('Explanation'), '  spaced  ')
    await user.click(screen.getByRole('button', { name: 'Confirm supersede' }))

    expect(onSubmit).toHaveBeenCalledTimes(1)
    const attempt = onSubmit.mock.calls[0][0] as FrozenAttempt
    expect(attempt.checkpointId).toBe(CP_A)
    expect(attempt.revision).toBe(2)
    expect(attempt.idempotencyKey).toBe('key-1')
    expect(attempt.body).toEqual({
      state: 'superseded',
      revision: 2,
      reason: { code: 'duplicate', explanation: '  spaced  ' },
    })
  })

  test('a superseded row offers restore with the restore code', async () => {
    const user = userEvent.setup()
    const { onSubmit } = renderHistory([entry({ state: 'superseded', revision: 1 })])

    await user.click(screen.getByRole('button', { name: `Restore ${CP_A}` }))
    expect(screen.queryByLabelText('Supersede reason code')).toBeNull()
    await user.type(screen.getByLabelText('Explanation'), 'bring it back')
    await user.click(screen.getByRole('button', { name: 'Confirm restore' }))

    const attempt = onSubmit.mock.calls[0][0] as FrozenAttempt
    expect(attempt.body.state).toBe('active')
    expect(attempt.body.reason.code).toBe('restore')
    expect(attempt.body.revision).toBe(1)
  })
})

describe('failure surfaces', () => {
  test('a conflict is displayed and never resubmits by itself', () => {
    const { onSubmit } = renderHistory([entry()], {
      conflictMessage: 'The checkpoint changed. Review the refreshed history.',
    })
    expect(screen.getByRole('alert')).toHaveTextContent('The checkpoint changed')
    expect(onSubmit).not.toHaveBeenCalled()
  })

  test('ambiguity exposes an explicit same-snapshot retry', async () => {
    const user = userEvent.setup()
    const pendingRetry: FrozenAttempt = {
      owner: 'owner-1',
      checkpointId: CP_A,
      revision: 2,
      body: { state: 'superseded', revision: 2, reason: { code: 'incorrect', explanation: 'x' } },
      idempotencyKey: 'key-frozen',
    }
    const onRetry = vi.fn()
    renderHistory([entry()], { pendingRetry, onRetry })

    await user.click(screen.getByRole('button', { name: 'Retry the same request' }))
    // The same snapshot and the same key, not a rebased one.
    expect(onRetry).toHaveBeenCalledExactlyOnceWith(pendingRetry)
  })
})

describe('staged intent stays bound to the row it was opened on', () => {
  test('a refresh to a new revision and state drops the open form', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn()
    const props = {
      audit: audit([entry({ revision: 0, state: 'active' as const })]),
      date: '2026-09-03',
      onSubmit,
      owner: 'owner-1',
      createIdempotencyKey: () => 'key-1',
    }
    const view = render(<CheckpointHistory {...props} />)

    await user.click(screen.getByRole('button', { name: `Supersede ${CP_A}` }))
    expect(screen.getByLabelText('Explanation')).toBeVisible()

    // The audit advances underneath: the staged Supersede must not become a
    // Restore submitted against revision 1.
    view.rerender(
      <CheckpointHistory
        {...props}
        audit={audit([entry({ revision: 1, state: 'superseded' })])}
      />,
    )
    expect(screen.queryByLabelText('Explanation')).toBeNull()
    expect(screen.getByRole('button', { name: `Restore ${CP_A}` })).toBeEnabled()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  test('no new action can start while an ambiguous attempt is unresolved', () => {
    renderHistory([entry()], {
      pendingRetry: {
        owner: 'owner-1',
        checkpointId: CP_A,
        revision: 0,
        body: { state: 'superseded', revision: 0, reason: { code: 'incorrect', explanation: 'x' } },
        idempotencyKey: 'key-frozen',
      },
      onRetry: vi.fn(),
    })
    expect(screen.getByRole('button', { name: `Supersede ${CP_A}` })).toBeDisabled()
  })
})

describe('owner lifetime and failed explanations', () => {
  test('an owner change cancels the staged confirmation rather than hiding it', async () => {
    const user = userEvent.setup()
    const props = {
      audit: audit([entry()]),
      date: '2026-09-03',
      onSubmit: vi.fn(),
      createIdempotencyKey: () => 'key-1',
    }
    const view = render(<CheckpointHistory {...props} owner="ws-A|2026-09-03" />)
    await user.click(screen.getByRole('button', { name: `Supersede ${CP_A}` }))
    await user.type(screen.getByLabelText('Explanation'), 'staged')

    // Day away and back, and workspace A -> B -> A, are new owners each time.
    view.rerender(<CheckpointHistory {...props} owner="ws-A|2026-09-02" />)
    view.rerender(<CheckpointHistory {...props} owner="ws-A|2026-09-03" />)
    expect(screen.queryByLabelText('Explanation')).toBeNull()

    view.rerender(<CheckpointHistory {...props} owner="ws-B|2026-09-03" />)
    view.rerender(<CheckpointHistory {...props} owner="ws-A|2026-09-03" />)
    expect(screen.queryByLabelText('Explanation')).toBeNull()
  })

  test('the raw explanation stays visible after a determinate refusal', () => {
    renderHistory([entry()], {
      conflictMessage: 'The checkpoint changed. Review the refreshed history.',
      failedExplanation: '  spaced  ',
    })
    // Verbatim, including the whitespace the server would have normalized.
    expect(screen.getByLabelText('Submitted explanation')).toHaveValue('  spaced  ')
  })

  test('ambiguity displays the frozen snapshot explanation with its retry', () => {
    renderHistory([entry()], {
      pendingRetry: {
        owner: 'owner-1',
        checkpointId: CP_A,
        revision: 0,
        body: { state: 'superseded', revision: 0, reason: { code: 'incorrect', explanation: '  frozen  ' } },
        idempotencyKey: 'key-frozen',
      },
      onRetry: vi.fn(),
    })
    expect(screen.getByLabelText('Frozen explanation')).toHaveValue('  frozen  ')
    expect(screen.getByRole('button', { name: 'Retry the same request' })).toBeVisible()
  })
})
