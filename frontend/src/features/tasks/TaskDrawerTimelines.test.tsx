import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { microsoftProviderGates } from '../../config/providerGates'
import type { TaskDetail } from '../../domain/types'
import { TaskActivityTimeline, TaskContextTimeline } from './TaskDrawerTimelines'

test('renders shared notes and Captures with stable typed keys and date-only ISO text', () => {
  const note = {
    id: 'same', ref: { kind: 'note', id: 'same' }, date_precision: 'date',
    text: 'Shared note', created: '2026-09-02', connections: [],
  } as TaskDetail['context'][number]
  const capture = {
    id: 'same', ref: { kind: 'capture', id: 'same' }, date_precision: 'instant',
    created_at: '2026-09-02T01:00:00Z', source: { display_title: 'Captured source' },
    normalized: { context: 'Reviewed source text' }, connections: [],
  } as TaskDetail['context'][number]
  const warning = vi.spyOn(console, 'error').mockImplementation(() => {})
  try {
    const rendered = render(<TaskContextTimeline context={[note, capture]} providerGates={microsoftProviderGates} />)
    expect(screen.getByRole('heading', { name: 'Shared note' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Captured source' })).toBeInTheDocument()
    const date = screen.getByText('2026-09-02')
    expect(date.tagName).toBe('TIME')
    expect(date).toHaveAttribute('datetime', '2026-09-02')
    const noteElement = screen.getByRole('heading', { name: 'Shared note' }).closest('article')
    rendered.rerender(<TaskContextTimeline context={[capture, note]} providerGates={microsoftProviderGates} />)
    expect(screen.getByRole('heading', { name: 'Shared note' }).closest('article')).toBe(noteElement)
    expect(warning).not.toHaveBeenCalled()
  } finally {
    warning.mockRestore()
  }
})

test('keeps missing dates unknown and preserves legacy source-link and reply gates', () => {
  const context = [{
    id: 'unknown', ref: { kind: 'note', id: 'unknown' }, date_precision: 'unknown',
    text: 'Unknown date note', created: 'not-a-date', connections: [],
  }, {
    id: 'legacy', text: 'Legacy note', created: '2026-08-31',
  }] as TaskDetail['context']
  render(<TaskContextTimeline context={context} providerGates={microsoftProviderGates} />)
  expect(screen.getByText('Unknown time')).not.toHaveAttribute('datetime')
  expect(screen.getByRole('heading', { name: 'Legacy note' })).toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})

test('renders planning-status activity with its established label and provenance', () => {
  const activity = [{
    type: 'task.planning_status',
    prior_status: 'started',
    status: 'done',
    prior_revision: 4,
    new_revision: 5,
    actor: 'user',
    provenance: 'api.v1',
    created_at: '2026-08-31T00:00:00Z',
  }] as TaskDetail['activity']

  render(<TaskActivityTimeline activity={activity} />)

  expect(screen.getByRole('heading', { name: 'In progress → Done' })).toBeInTheDocument()
  expect(screen.getByText('Revision 4 → 5')).toBeInTheDocument()
  expect(screen.getByText('By user · api.v1')).toBeInTheDocument()
})

test('renders reviewed external context and only exposes a safe source link', () => {
  const context = [{
    id: 'C-0001',
    kind: 'capture',
    created_at: '2026-08-31T00:00:00Z',
    source: {
      provider: 'microsoft-outlook',
      display_title: 'Release decision',
      web_url: 'https://outlook.office.com/mail/id/example',
    },
    normalized: { context: 'Decision context' },
  }] as TaskDetail['context']

  render(<TaskContextTimeline context={context} providerGates={microsoftProviderGates} />)

  expect(screen.getByRole('heading', { name: 'Release decision' })).toBeInTheDocument()
  expect(screen.getByText('Decision context')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Open source/ })).toHaveAttribute(
    'href',
    'https://outlook.office.com/mail/id/example',
  )
})
