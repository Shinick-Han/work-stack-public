import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { microsoftProviderGates } from '../../config/providerGates'
import type { TaskDetail } from '../../domain/types'
import { TaskActivityTimeline, TaskContextTimeline } from './TaskDrawerTimelines'

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
