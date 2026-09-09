import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, test, vi } from 'vitest'

import { TaskContextTimeline, type TaskContextLinkRemovalState } from './TaskContextTimeline'
import { TaskContextTimeline as featureTimeline } from '../features/tasks/TaskDrawerTimelines'
import { contextTitle, externalContext } from '../utils/taskContext'
import { contextTitle as featureContextTitle, externalContext as featureExternalContext } from '../features/tasks/taskDrawerModel'
import { microsoftProviderGates } from '../config/providerGates'
import type { TaskDetail } from '../domain/types'

/**
 * Q4 acceptance: the renderer MOVED here and the Task feature re-exports it, so
 * both paths must be the same component and the same helpers. The rendering
 * controls exercise the real component - nothing here is mocked.
 */

const item = (patch: Record<string, unknown>) => patch as TaskDetail['context'][number]

describe('the shared renderer is the one the Task feature exports', () => {
  it('is identical across both paths, helpers included', () => {
    expect(featureTimeline).toBe(TaskContextTimeline)
    expect(featureContextTitle).toBe(contextTitle)
    expect(featureExternalContext).toBe(externalContext)
  })
})

describe('the shared renderer keeps its established markup', () => {
  it('retains a note and a Capture that share a raw ID but differ by ref kind', () => {
    const note = item({
      id: 'same',
      ref: { kind: 'note', id: 'same' },
      date_precision: 'date',
      text: 'Shared note',
      created: '2026-09-02',
      connections: [],
    })
    const capture = item({
      id: 'same',
      ref: { kind: 'capture', id: 'same' },
      date_precision: 'instant',
      created_at: '2026-09-02T01:00:00Z',
      source: { display_title: 'Captured source', provider: 'microsoft-outlook' },
      normalized: { context: 'Reviewed source text', action_items: [{ id: 'A-1', title: 'Reply today' }] },
      connections: [],
    })

    render(<TaskContextTimeline context={[note, capture]} providerGates={microsoftProviderGates} />)

    expect(screen.getAllByRole('article')).toHaveLength(2)
    expect(screen.getByRole('heading', { name: 'Shared note' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Captured source' })).toBeInTheDocument()
    // Date-only precision keeps the raw ISO day in a machine-readable time.
    const day = screen.getByText('2026-09-02')
    expect(day.tagName).toBe('TIME')
    expect(day).toHaveAttribute('datetime', '2026-09-02')
    // Normalized action items render as list entries.
    expect(screen.getByRole('listitem')).toHaveTextContent('Reply today')
  })

  it('shows unknown precision without a machine date and the provider gate pill', () => {
    const unknown = item({
      id: 'unknown',
      ref: { kind: 'note', id: 'unknown' },
      date_precision: 'unknown',
      text: 'Unknown date note',
      created: 'not-a-date',
      connections: [],
    })
    const gated = item({
      id: 'gated',
      ref: { kind: 'capture', id: 'gated' },
      date_precision: 'instant',
      created_at: '2026-09-02T01:00:00Z',
      source: { display_title: 'Gated capture', provider: 'microsoft-teams' },
      connections: [],
    })

    render(<TaskContextTimeline context={[unknown, gated]} providerGates={microsoftProviderGates} />)

    expect(screen.getByText('Unknown time')).not.toHaveAttribute('datetime')
    expect(screen.getByText('Reply unavailable · Gate 0 pending')).toBeInTheDocument()
  })

  it('drops an unsafe source URL and keeps a safe one sandboxed', () => {
    const unsafe = item({
      id: 'unsafe',
      ref: { kind: 'capture', id: 'unsafe' },
      source: { display_title: 'Unsafe', web_url: 'javascript:alert(1)' },
      connections: [],
    })

    const view = render(<TaskContextTimeline context={[unsafe]} providerGates={microsoftProviderGates} />)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()

    const safe = item({
      id: 'safe',
      ref: { kind: 'capture', id: 'safe' },
      source: { display_title: 'Safe', web_url: 'https://example.test/thread' },
      connections: [],
    })
    view.rerender(<TaskContextTimeline context={[safe]} providerGates={microsoftProviderGates} />)

    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://example.test/thread')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('falls back to the exact established title and renders the empty state', () => {
    render(<TaskContextTimeline context={[item({ id: 'bare', connections: [] })]} providerGates={microsoftProviderGates} />)
    expect(screen.getByRole('heading', { name: 'Context item' })).toBeInTheDocument()

    render(<TaskContextTimeline context={[]} providerGates={microsoftProviderGates} />)
    expect(screen.getByText('No context yet')).toBeInTheDocument()
  })

  it('renders a plain-text note once and keeps a real source title beside distinct body copy', () => {
    const note = item({
      id: 'plain',
      ref: { kind: 'note', id: 'plain' },
      text: 'Ship the resume brief without repeating this paragraph.',
      connections: [],
    })
    const titled = item({
      id: 'titled',
      ref: { kind: 'capture', id: 'titled' },
      source: { display_title: 'Mail subject' },
      text: 'Ship the resume brief without repeating this paragraph.',
      connections: [],
    })

    render(<TaskContextTimeline context={[note, titled]} providerGates={microsoftProviderGates} />)

    expect(screen.getAllByText('Ship the resume brief without repeating this paragraph.')).toHaveLength(2)
    expect(screen.getByRole('heading', { name: 'Ship the resume brief without repeating this paragraph.' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Mail subject' })).toBeInTheDocument()
    const noteArticle = screen.getByRole('heading', { name: 'Ship the resume brief without repeating this paragraph.' }).closest('article')
    expect(noteArticle?.querySelector('p')).toBeNull()
  })

  it('keeps unknown extra fields inspectable without inventing titles', () => {
    render(<TaskContextTimeline context={[item({
      id: 'extra',
      ref: { kind: 'note', id: 'extra' },
      text: 'Known note',
      ticket: { id: 'WS-41' },
      connections: [],
    })]} providerGates={microsoftProviderGates} />)

    expect(screen.getByRole('heading', { name: 'Known note' })).toBeInTheDocument()
    expect(screen.getByText('Additional recorded data (1)')).toBeInTheDocument()
    expect(screen.getByText('ticket')).toBeInTheDocument()
    expect(screen.getByText('{"id":"WS-41"}')).toBeInTheDocument()
  })

  it('discloses unrendered recorded provenance, connections, source attributes and tags', () => {
    render(<TaskContextTimeline context={[item({
      id: 'recorded',
      ref: { kind: 'capture', id: 'recorded' },
      source: {
        provider: 'microsoft-outlook',
        display_title: 'Mail subject',
        web_url: 'https://example.test/thread',
        resource_type: 'message',
        fingerprint: 'sha256:abc',
        extra_attr: 'nested-unknown',
      },
      normalized: {
        context: 'Mail subject',
        tags: ['resume', 'brief'],
      },
      text: 'Reviewed the thread after the heading was reused.',
      provenance: { capture_mode: 'manual', raw_retained: false },
      connections: [{ target: { kind: 'task', id: 'T-1' }, reasons: ['capture-link'] }],
    })]} providerGates={microsoftProviderGates} />)

    expect(screen.getByRole('heading', { name: 'Mail subject' })).toBeInTheDocument()
    expect(screen.getByText('Reviewed the thread after the heading was reused.')).toBeInTheDocument()
    expect(screen.getByText('Additional recorded data (7)')).toBeInTheDocument()
    expect(screen.getByText('provenance.capture_mode')).toBeInTheDocument()
    expect(screen.getByText('manual')).toBeInTheDocument()
    expect(screen.getByText('provenance.raw_retained')).toBeInTheDocument()
    expect(screen.getByText('false')).toBeInTheDocument()
    expect(screen.getByText('connections')).toBeInTheDocument()
    expect(screen.getByText('source.resource_type')).toBeInTheDocument()
    expect(screen.getByText('source.fingerprint')).toBeInTheDocument()
    expect(screen.getByText('source.extra_attr')).toBeInTheDocument()
    expect(screen.getByText('nested-unknown')).toBeInTheDocument()
    expect(screen.getByText('normalized.tags')).toBeInTheDocument()
    expect(screen.getByText('["resume","brief"]')).toBeInTheDocument()
    const extras = screen.getByText('Additional recorded data (7)').closest('details')
    expect(extras?.textContent).not.toContain('Mail subject')
    expect(screen.getByRole('link', { name: /Open source/ })).toBeInTheDocument()
  })
})

describe('R22 removal presentation is bound to the card the reader is looking at', () => {
  const linked = item({
    id: 'C-0001',
    ref: { kind: 'capture', id: 'C-0001' },
    revision: 5,
    date_precision: 'instant',
    created_at: '2026-09-02T01:00:00Z',
    source: { display_title: 'Captured source', provider: 'microsoft-outlook' },
    connections: [{ target: { kind: 'task', id: 'T-0001' }, reasons: ['capture-link'] }],
  })

  const removal = (patch: Partial<TaskContextLinkRemovalState> = {}): TaskContextLinkRemovalState => ({
    taskId: 'T-0001',
    pending: null,
    failure: null,
    undo: null,
    locked: false,
    onRemove: () => undefined,
    onUndo: () => undefined,
    onDismissUndo: () => undefined,
    ...patch,
  })

  test('renders the action for the Task own link and calls back with the displayed revision', async () => {
    const onRemove = vi.fn()
    render(
      <TaskContextTimeline
        context={[linked]}
        providerGates={microsoftProviderGates}
        removal={removal({ onRemove })}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Remove task link' }))

    expect(onRemove).toHaveBeenCalledExactlyOnceWith({ captureId: 'C-0001', revision: 5 })
  })

  test('ignores a pending row or a message recorded against another revision', () => {
    render(
      <TaskContextTimeline
        context={[linked]}
        providerGates={microsoftProviderGates}
        removal={removal({
          failure: { captureId: 'C-0001', revision: 4, message: 'Stale message', retry: true },
        })}
      />,
    )

    // The card has moved on to revision 5, so the revision-4 attempt decorates nothing.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button', { name: 'Remove task link' })).toBeEnabled()
  })

  test('locks every card while one request is in flight', () => {
    render(
      <TaskContextTimeline
        context={[linked]}
        providerGates={microsoftProviderGates}
        removal={removal({ locked: true, pending: { captureId: 'C-9999', revision: 1, retry: false } })}
      />,
    )

    expect(screen.getByRole('button', { name: 'Remove task link' })).toBeDisabled()
    expect(screen.queryByRole('status')).toBeNull()
  })
})
