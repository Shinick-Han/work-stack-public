import type { ComponentProps } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import type { Task, WorkspaceProjection } from '../../domain/types'
import { task, workspace } from '../../test/fixtures'
import { FocusPage } from './FocusPage'

function makeTask(overrides: Partial<Task>): Task {
  return {
    ...task,
    id: 'T-0100',
    objective_ids: [],
    context_count: 0,
    ...overrides,
  }
}

function makeWorkspace(tasks: Task[]): WorkspaceProjection {
  return { ...workspace, tasks }
}

function renderPage(tasks: Task[], overrides: Partial<ComponentProps<typeof FocusPage>> = {}) {
  const props: ComponentProps<typeof FocusPage> = {
    workspace: makeWorkspace(tasks),
    today: '2026-08-29',
    isRefreshing: false,
    onRefresh: vi.fn(),
    onCreateTask: vi.fn(),
    onChangeTaskStatus: vi.fn().mockResolvedValue(undefined),
    onSelectTask: vi.fn(),
    ...overrides,
  }
  return { ...render(<FocusPage {...props} />), props }
}

test('renders each candidate once while preserving every focus reason', () => {
  const overlap = makeTask({
    id: 'T-0101',
    title: 'Resolve launch decision',
    status: 'started',
    priority: 'P0',
    due: '2026-08-30',
  })
  const quiet = makeTask({
    id: 'T-0102',
    title: 'Later maintenance',
    status: 'open',
    priority: 'P2',
    due: '2026-09-06',
  })
  const terminal = makeTask({
    id: 'T-0103',
    title: 'Already finished',
    status: 'done',
    priority: 'P0',
    due: '2026-08-20',
  })

  renderPage([overlap, quiet, terminal])

  expect(screen.getByText('1', { selector: '.focus-summary strong' })).toBeInTheDocument()
  expect(screen.getByText('2', { selector: '.focus-summary strong' })).toBeInTheDocument()
  expect(screen.getAllByRole('article')).toHaveLength(1)
  expect(screen.getByText('Due in 1 day')).toBeInTheDocument()
  expect(screen.getByText('In progress')).toBeInTheDocument()
  expect(screen.getByText('P0')).toBeInTheDocument()
  expect(screen.queryByText('Later maintenance')).not.toBeInTheDocument()
  expect(screen.queryByText('Already finished')).not.toBeInTheDocument()
})

test('keeps the article passive while exposing an explicit revision-safe Start action', async () => {
  const onSelectTask = vi.fn()
  const onRefresh = vi.fn()
  const onCreateTask = vi.fn()
  const onChangeTaskStatus = vi.fn().mockResolvedValue(undefined)
  renderPage([
    makeTask({ id: 'T-0104', title: 'Review invalid schedule', due: '2026-02-30', priority: 'P2', status: 'open' }),
  ], { onSelectTask, onRefresh, onCreateTask, onChangeTaskStatus, isRefreshing: true })

  const article = screen.getByRole('article')
  expect(article).not.toHaveAttribute('tabindex')
  expect(article).not.toHaveAttribute('role', 'button')
  expect(screen.getByText('Due date needs review')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Start' }))

  await userEvent.click(screen.getByRole('button', { name: 'Open T-0104 · Review invalid schedule' }))
  await userEvent.click(screen.getByRole('button', { name: 'New task' }))
  expect(screen.getByRole('button', { name: 'Refresh focus' })).toBeDisabled()
  expect(onSelectTask).toHaveBeenCalledOnce()
  expect(onSelectTask).toHaveBeenCalledWith('T-0104')
  expect(onCreateTask).toHaveBeenCalledOnce()
  expect(onChangeTaskStatus).toHaveBeenCalledWith('T-0104', 'started')
  expect(onRefresh).not.toHaveBeenCalled()
})

test('explains unfinished dependencies and prevents starting a blocked task', async () => {
  const onSelectTask = vi.fn()
  const onChangeTaskStatus = vi.fn().mockResolvedValue(undefined)
  renderPage([
    makeTask({ id: 'T-0110', title: 'Ship dependent result', priority: 'P0', dependencies: ['T-0111'] }),
    makeTask({ id: 'T-0111', title: 'Complete prerequisite', priority: 'P2', status: 'open' }),
  ], { onChangeTaskStatus, onSelectTask })

  expect(screen.getByText('Blocked by T-0111')).toBeInTheDocument()
  expect(screen.getByText('Complete prerequisite')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Blocked' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Open blocker T-0111' }))
  expect(onSelectTask).toHaveBeenCalledWith('T-0111')
  expect(onChangeTaskStatus).not.toHaveBeenCalled()
})

test('uses the shared empty state and offers only task creation', async () => {
  const onCreateTask = vi.fn()
  renderPage([
    makeTask({ id: 'T-0105', status: 'open', priority: 'P2', due: null }),
    makeTask({ id: 'T-0106', status: 'dropped', priority: 'P0', due: '2026-08-20' }),
  ], { onCreateTask })

  expect(screen.getByRole('heading', { name: 'No focus candidates' })).toBeInTheDocument()
  expect(screen.queryByRole('article')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /workspace/i })).not.toBeInTheDocument()
  await userEvent.click(screen.getAllByRole('button', { name: 'New task' })[0])
  expect(onCreateTask).toHaveBeenCalledOnce()
})

test('contains invalid workspace identity errors instead of crashing the app', () => {
  const duplicate = makeTask({ id: 'T-0107', title: 'Duplicate identity', priority: 'P1' })
  renderPage([duplicate, { ...duplicate }])

  expect(screen.getByRole('alert')).toHaveTextContent(/duplicate/i)
  expect(screen.queryByRole('article')).not.toBeInTheDocument()
})

test('keeps a focus session separate from planning status and records its worklog explicitly', async () => {
  const onStartWorkSession = vi.fn().mockResolvedValue(undefined)
  const onTransitionWorkSession = vi.fn().mockResolvedValue(undefined)
  const onRecordWorkSession = vi.fn().mockResolvedValue(undefined)
  const candidate = makeTask({
    id: 'T-0120',
    title: 'Write the decision note',
    priority: 'P0',
    status: 'open',
  })
  const { rerender, props } = renderPage([candidate], {
    workSessions: { current: null, pending: [] },
    isWorkSessionPending: false,
    onStartWorkSession,
    onTransitionWorkSession,
    onRecordWorkSession,
  })

  await userEvent.click(screen.getByRole('button', { name: 'Begin work session for T-0120' }))
  expect(onStartWorkSession).toHaveBeenCalledWith('T-0120')
  expect(props.onChangeTaskStatus).not.toHaveBeenCalled()

  rerender(<FocusPage
    {...props}
    workSessions={{
      current: {
        id: 'WS-000001', task_id: 'T-0120', task: candidate.title, date: '2026-08-29',
        state: 'running', started_at: '2026-08-29T09:00:00Z', updated_at: '2026-08-29T09:00:00Z',
        elapsed_seconds: 125, worklog_state: 'not_ready',
      },
      pending: [],
    }}
  />)
  expect(screen.getByText('Current work session')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Pause session' }))
  expect(onTransitionWorkSession).toHaveBeenCalledWith('WS-000001', 'pause')

  rerender(<FocusPage
    {...props}
    workSessions={{
      current: null,
      pending: [{
        id: 'WS-000001', task_id: 'T-0120', task: candidate.title, date: '2026-08-29',
        state: 'stopped', started_at: '2026-08-29T09:00:00Z', updated_at: '2026-08-29T09:02:05Z',
        elapsed_seconds: 125, worklog_state: 'pending',
      }],
    }}
  />)
  await userEvent.type(screen.getByLabelText('Done'), 'Drafted the decision note')
  await userEvent.click(screen.getByRole('button', { name: 'Add to worklog' }))
  expect(onRecordWorkSession).toHaveBeenCalledWith('WS-000001', {
    done: ['Drafted the decision note'], next: [], blockers: [],
  })
})
