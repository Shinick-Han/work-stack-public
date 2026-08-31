import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import type { Objective, Task } from '../../domain/types'
import { workspace } from '../../test/fixtures'
import { ObjectiveHubPage } from './ObjectiveHubPage'

function jsonResponse(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }))
}

test('adds and edits an Objective and Key Result with revision-guarded mutations', async () => {
  let objective: Objective = { ...workspace.objectives[0], key_results: [] }
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url === '/api/v1/objectives/O-1' && !init?.method) return jsonResponse({
      data: { objective, tasks: workspace.tasks, activity: [] },
    })
    if (url === '/api/v1/objectives/O-1/key-results' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      objective = {
        ...objective,
        revision: 1,
        key_results: [{ id: 'KR-1', text: body.text, target: body.target, progress: 0, status: 'active' }],
      }
      return jsonResponse({ data: objective, meta: { replayed: false } }, 201)
    }
    if (url === '/api/v1/objectives/O-1/key-results/KR-1' && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body))
      objective = {
        ...objective,
        revision: 2,
        key_results: [{ id: 'KR-1', text: body.text, target: body.target, progress: body.progress, status: body.status }],
      }
      return jsonResponse({ data: objective })
    }
    if (url === '/api/v1/objectives/O-1' && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body))
      objective = { ...objective, objective: body.objective, quarter: body.quarter, revision: 3 }
      return jsonResponse({ data: objective })
    }
    if (url === '/api/v1/workspace') return jsonResponse({ data: { ...workspace, objectives: [objective] } })
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onNotice = vi.fn()
  const onCreateAlignedTask = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><ObjectiveHubPage objectiveId="O-1" onCreateAlignedTask={onCreateAlignedTask} onNotice={onNotice} onOpenTask={vi.fn()} onSelectObjective={vi.fn()} workspace={workspace} /></QueryClientProvider>)

  expect(await screen.findByText(workspace.tasks[0].title)).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Create aligned task' }))
  expect(onCreateAlignedTask).toHaveBeenCalledWith('O-1')
  await userEvent.type(screen.getByLabelText('New Key Result'), 'Review five real work days')
  await userEvent.type(screen.getByLabelText('Target label'), '5 days')
  await userEvent.click(screen.getByRole('button', { name: 'Add Key Result' }))

  await waitFor(() => expect(onNotice).toHaveBeenCalledWith('Key Result added'))
  expect(await screen.findByLabelText('Key Result description')).toHaveValue('Review five real work days')
  const mutation = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/key-results') && init?.method === 'POST')
  expect((mutation?.[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^workstack:/)
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({
    text: 'Review five real work days', target: '5 days', revision: 0,
  })

  await userEvent.clear(screen.getByLabelText('Key Result description'))
  await userEvent.type(screen.getByLabelText('Key Result description'), 'Review seven real work days')
  await userEvent.clear(screen.getByLabelText('Key Result target'))
  await userEvent.type(screen.getByLabelText('Key Result target'), '7 days')
  await userEvent.click(screen.getByRole('button', { name: 'Save KR' }))
  await waitFor(() => expect(onNotice).toHaveBeenCalledWith('Key Result updated'))

  await userEvent.clear(screen.getByLabelText('Objective title'))
  await userEvent.type(screen.getByLabelText('Objective title'), 'Make execution reviewable every week')
  await userEvent.clear(screen.getByLabelText('Objective quarter'))
  await userEvent.type(screen.getByLabelText('Objective quarter'), '2026-Q4')
  await userEvent.click(screen.getByRole('button', { name: 'Save Objective' }))
  await waitFor(() => expect(onNotice).toHaveBeenCalledWith('Objective updated'))

  const krMutation = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/key-results/KR-1') && init?.method === 'PATCH')
  expect(JSON.parse(String(krMutation?.[1]?.body))).toEqual({
    text: 'Review seven real work days', target: '7 days', progress: 0, status: 'active', revision: 1,
  })
  const objectiveMutation = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/objectives/O-1') && init?.method === 'PATCH')
  expect(JSON.parse(String(objectiveMutation?.[1]?.body))).toEqual({
    objective: 'Make execution reviewable every week', quarter: '2026-Q4', revision: 2,
  })
})

test('creates an idempotent Objective directly from the Hub and selects it', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url === '/api/v1/objectives' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      return jsonResponse({ data: {
        id: 'O-2', objective: body.objective, quarter: body.quarter,
        status: 'active', key_results: [], revision: 0,
      }, meta: { replayed: false } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onSelectObjective = vi.fn()
  const onNotice = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><ObjectiveHubPage objectiveId="all" onCreateAlignedTask={vi.fn()} onNotice={onNotice} onOpenTask={vi.fn()} onSelectObjective={onSelectObjective} workspace={{ ...workspace, objectives: [] }} /></QueryClientProvider>)

  await userEvent.click(screen.getByRole('button', { name: 'New Objective' }))
  await userEvent.type(screen.getByLabelText('New Objective title'), 'Make planning calm')
  await userEvent.clear(screen.getByLabelText('New Objective quarter'))
  await userEvent.type(screen.getByLabelText('New Objective quarter'), '2026-Q4')
  await userEvent.click(screen.getByRole('button', { name: 'Create Objective' }))

  await waitFor(() => expect(onSelectObjective).toHaveBeenCalledWith('O-2'))
  expect(onNotice).toHaveBeenCalledWith('Objective O-2 added')
  const mutation = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/objectives') && init?.method === 'POST')
  expect((mutation?.[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^workstack:/)
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({ objective: 'Make planning calm', quarter: '2026-Q4' })
})

test('projects actionable and blocked execution health for an Objective', async () => {
  const base = workspace.tasks[0]
  const prerequisite: Task = { ...base, id: 'T-0200', uid: '20000000-0000-4000-8000-000000000000', title: 'Finish prerequisite', objective_ids: [], status: 'open', dependencies: [] }
  const blocked: Task = { ...base, id: 'T-0201', uid: '20100000-0000-4000-8000-000000000000', title: 'Blocked outcome', objective_ids: ['O-1'], status: 'open', dependencies: [prerequisite.id] }
  const actionable: Task = { ...base, id: 'T-0202', uid: '20200000-0000-4000-8000-000000000000', title: 'Actionable outcome', objective_ids: ['O-1'], status: 'started', dependencies: [] }
  const done: Task = { ...base, id: 'T-0203', uid: '20300000-0000-4000-8000-000000000000', title: 'Completed outcome', objective_ids: ['O-1'], status: 'done', dependencies: [] }
  const tasks = [prerequisite, blocked, actionable, done]
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input) === '/api/v1/objectives/O-1') return jsonResponse({
      data: { objective: workspace.objectives[0], tasks: [blocked, actionable, done], activity: [] },
    })
    throw new Error(`Unexpected request: ${String(input)}`)
  }))
  const onOpenTask = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ObjectiveHubPage objectiveId="O-1" onCreateAlignedTask={vi.fn()} onNotice={vi.fn()} onOpenTask={onOpenTask} onSelectObjective={vi.fn()} workspace={{ ...workspace, tasks }} /></QueryClientProvider>)

  const readiness = await screen.findByLabelText('Objective execution readiness')
  expect(readiness).toHaveTextContent('Actionable1')
  expect(readiness).toHaveTextContent('Blocked1')
  expect(readiness).toHaveTextContent('Done1')
  expect(readiness).toHaveTextContent('Dropped0')
  expect(screen.getByText('Waiting on T-0200')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /T-0201.*Blocked outcome/ }))
  expect(onOpenTask).toHaveBeenCalledWith('T-0201')
})
