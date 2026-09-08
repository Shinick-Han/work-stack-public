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

  test('a Task filter hides other tasks without deleting the day-wide audit', () => {
    const other = entry()
    renderHistory([
      entry(),
      entry({
        checkpoint_id: CP_B,
        locator: { ...other.locator, task_id: 'T-0033', ordinal: 1 },
        recorded: other.recorded
          ? { ...other.recorded, task_id: 'T-0033', ordinal: 1 }
          : null,
      }),
    ], { taskId: 'T-0033' })
    expect(screen.getByText(CP_B)).toBeVisible()
    expect(screen.queryByText(CP_A)).toBeNull()
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

  test('renders opaque entry data with a safe fallback', async () => {
    const user = userEvent.setup()
    const cyclic: Record<string, unknown> = {}
    cyclic.self = cyclic
    renderHistory([entry({ entry: cyclic })])

    // The body states the outcome; the unserializable field is named below it.
    expect(screen.getByText('No readable summary for this entry')).toBeVisible()
    await user.click(screen.getByText(/Additional recorded data/))
    expect(screen.getByText('self')).toBeVisible()
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

/**
 * The audit row is work someone recorded, not a wire payload. These cases pin
 * the readable summary AND the fact that identity and compensation are still
 * reachable next to it, so making the card human-facing cannot quietly cost the
 * audit its provenance or its controls.
 */
describe('a checkpoint reads as recorded work, not as a payload', () => {
  const WORKLOG = {
    task_id: 'T-0001',
    task: 'Define release quality gate',
    done: ['Drafted release gate sections'],
    next: ['Validate with engineering'],
    blockers: ['Waiting on legal'],
  }

  test('shows task, status and the done/next/blockers narrative', () => {
    renderHistory([entry({ entry: WORKLOG })])

    expect(screen.getByText('Define release quality gate')).toBeVisible()
    expect(screen.getByText('T-0001')).toBeVisible()
    expect(screen.getByText('Active')).toBeVisible()
    for (const label of ['Done', 'Next', 'Blockers']) {
      expect(screen.getByText(label).tagName).toBe('DT')
    }
    expect(screen.getByText('Drafted release gate sections')).toBeVisible()
    expect(screen.getByText('Validate with engineering')).toBeVisible()
    expect(screen.getByText('Waiting on legal')).toBeVisible()
  })

  test('never prints the serialized payload for a row it can read', () => {
    renderHistory([entry({ entry: WORKLOG })])
    // The old presentation was JSON.stringify of the whole row.
    expect(screen.queryByText(/\{"task_id"/)).toBeNull()
    expect(document.body.textContent).not.toContain('{"')
  })

  test('an empty list says so rather than disappearing', () => {
    renderHistory([entry({ entry: { ...WORKLOG, blockers: [] } })])
    // Blockers is still a labelled row, so "none" is a fact, not an omission.
    expect(screen.getByText('Blockers')).toBeVisible()
    expect(screen.queryByText('Waiting on legal')).toBeNull()
    expect(screen.getAllByText('None recorded')).toHaveLength(1)
  })

  test('a superseded row says Superseded in words', () => {
    renderHistory([entry({ entry: WORKLOG, state: 'superseded', revision: 1 })])
    expect(screen.getByText('Superseded')).toBeVisible()
    expect(screen.queryByText('Active')).toBeNull()
  })

  test('the status chip carries an accessible label, not just colour', () => {
    renderHistory([entry({ entry: WORKLOG })])
    const status = screen.getByText('Status')
    expect(status).toHaveClass('sr-only')
    expect(status.parentElement).toHaveTextContent('Status Active')
  })

  test('a payload with no task keeps a stated heading', () => {
    renderHistory([entry({ entry: { done: ['shipped'] } })])
    expect(screen.getByText('Untitled checkpoint entry')).toBeVisible()
    expect(screen.getByText('shipped')).toBeVisible()
  })

  test('a legacy free-text payload is shown verbatim', () => {
    renderHistory([entry({ entry: '  shipped the gate  ' })])
    expect(screen.getByText('shipped the gate')).toHaveTextContent('shipped the gate')
  })
})

describe('identity and compensation stay available beside the summary', () => {
  const WORKLOG = { task_id: 'T-0001', task: 'Define release quality gate', done: ['a'] }

  test('checkpoint id, ordinal and revision remain on the card, set apart', () => {
    renderHistory([entry({ entry: WORKLOG, revision: 3 })])
    const identity = screen.getByText(CP_A)
    const provenance = identity.closest('p')
    expect(provenance).not.toBeNull()
    expect(provenance).toHaveClass('checkpoint-entry__identifiers')
    expect(provenance).toHaveTextContent('ordinal 0')
    expect(provenance).toHaveTextContent('revision 3')
    // Provenance is not the heading: the Task is.
    expect(provenance).not.toContainElement(screen.getByText('Define release quality gate'))
  })

  test('supersede is still offered on a readable row, as a secondary action', () => {
    renderHistory([entry({ entry: WORKLOG })])
    const action = screen.getByRole('button', { name: `Supersede ${CP_A}` })
    expect(action).toBeEnabled()
    expect(action).toHaveClass('checkpoint-entry__action')
    expect(action).toHaveClass('button--ghost')
  })

  test('an unreadable payload states so and lists its fields, never a JSON body', () => {
    renderHistory([entry({ entry: { shape: 'unknown' }, revision: 2 })])
    // The body says what happened instead of printing the serialized record.
    expect(screen.getByText('No readable summary for this entry')).toBeVisible()
    expect(screen.queryByText('{"shape":"unknown"}')).toBeNull()
    const panel = screen.getByText(/Additional recorded data/).closest('details')
    expect(panel).toHaveTextContent('shape')
    expect(panel).toHaveTextContent('unknown')
    // Identity and compensation survive an opaque payload untouched.
    expect(screen.getByText(CP_A)).toBeVisible()
    expect(screen.getByRole('button', { name: `Supersede ${CP_A}` })).toBeEnabled()
  })

  test('a transition reads as a sentence with its code and raw explanation', () => {
    renderHistory([
      entry({
        entry: WORKLOG,
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
            reason: { code: 'incorrect', explanation: '  Wrong day  ' },
            origin: 'agent-cli-v1',
          },
        ],
      }),
    ])
    const list = screen.getByRole('list', { name: `Transitions for ${CP_A}` })
    expect(within(list).getByRole('listitem')).toHaveTextContent(
      'Superseded at revision 1 · incorrect · Wrong day',
    )
  })
})

/**
 * Partial recognition must not cost the record anything. The readable summary
 * still leads, but a value the summary could not render has to remain
 * DISCOVERABLE on the card rather than being dropped on the floor.
 */
describe('unrecognized values stay reachable as secondary data', () => {
  const PARTIAL = { task_id: 'T-1', done: ['kept', 42], legacy_note: 'critical' }

  test('the reported case keeps 42 and critical discoverable under the summary', () => {
    renderHistory([entry({ entry: PARTIAL })])

    // The human summary is untouched and still leads the card.
    expect(screen.getByText('T-1')).toBeVisible()
    expect(screen.getByText('kept')).toBeVisible()

    const extras = screen.getByText(/Additional recorded data/)
    expect(extras.tagName).toBe('SUMMARY')
    const panel = extras.closest('details')
    expect(panel).toHaveClass('checkpoint-entry__extras')
    expect(panel).toHaveTextContent('done[1]')
    expect(panel).toHaveTextContent('42')
    expect(panel).toHaveTextContent('legacy_note')
    expect(panel).toHaveTextContent('critical')
  })

  test('the leftovers are secondary: collapsed, and never the card body', async () => {
    const user = userEvent.setup()
    renderHistory([entry({ entry: PARTIAL })])

    const panel = screen.getByText(/Additional recorded data/).closest('details')
    // Closed on arrival, so the recorded work is what the reader sees first.
    expect(panel).not.toHaveAttribute('open')
    // The body is still the Done/Next/Blockers list, not a JSON dump.
    expect(screen.getByText('Done').tagName).toBe('DT')
    expect(screen.queryByText(/^\{"task_id"/)).toBeNull()

    await user.click(screen.getByText(/Additional recorded data/))
    expect(panel).toHaveAttribute('open')
    expect(screen.getByText('42')).toBeVisible()
    expect(screen.getByText('critical')).toBeVisible()
  })

  test('a fully understood payload offers no leftovers panel at all', () => {
    renderHistory([entry({ entry: { task_id: 'T-1', task: 'Ship it', done: ['kept'] } })])
    expect(screen.queryByText(/Additional recorded data/)).toBeNull()
  })

  test('markup inside a leftover is shown as text, never parsed as HTML', () => {
    renderHistory([
      entry({ entry: { done: ['kept'], legacy_note: '<img src=x onerror="alert(1)">' } }),
    ])
    const panel = screen.getByText(/Additional recorded data/).closest('details')
    expect(panel).toHaveTextContent('<img src=x onerror="alert(1)">')
    // The characters reached the DOM as text: no element was ever created.
    expect(document.querySelector('img')).toBeNull()
  })
})

/**
 * A null slot has to be findable on the CARD, not just in the model: the whole
 * point of the finding is that a reviewer looking at the audit can see the
 * record was written with a hole in it.
 */
describe('null list slots are discoverable on the card', () => {
  test('a mixed payload shows done[1]: null beside the bullets it kept', async () => {
    const user = userEvent.setup()
    renderHistory([
      entry({
        entry: {
          task_id: 'T-1',
          task: 'Ship the gate',
          done: ['kept', null, 42],
          next: null,
          legacy_note: 'critical',
        },
      }),
    ])

    // The readable summary still leads and is unaffected by the leftovers.
    expect(screen.getByText('Ship the gate')).toBeVisible()
    expect(screen.getByText('kept')).toBeVisible()

    await user.click(screen.getByText(/Additional recorded data/))
    const panel = screen.getByText(/Additional recorded data/).closest('details')
    expect(panel).toHaveTextContent('done[1]')
    expect(panel).toHaveTextContent('done[2]')
    expect(panel).toHaveTextContent('legacy_note')
    // The null slot is rendered as React text, so it reads as the word null.
    expect(within(panel as HTMLElement).getAllByText('null')).toHaveLength(1)
    // `next: null` is a whole absent field and never becomes a row.
    expect(panel).not.toHaveTextContent('next')
  })
})
