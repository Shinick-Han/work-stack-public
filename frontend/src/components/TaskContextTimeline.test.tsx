import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TaskContextTimeline } from './TaskContextTimeline'
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
})
