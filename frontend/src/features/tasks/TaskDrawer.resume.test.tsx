import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { jsonResponse, task, workspace } from '../../test/fixtures'
import type { CheckpointAudit, TaskDetail, WorkspaceProjection } from '../../domain/types'
import { knowledgeHostAvailable, requestKnowledge } from '../knowledge/knowledgeHostBridge'
import { TaskDrawer } from './TaskDrawer'

vi.mock('../knowledge/knowledgeHostBridge', () => ({
  knowledgeHostAvailable: vi.fn(),
  requestKnowledge: vi.fn(),
}))

const WORKSPACE_UID = '123e4567-e89b-42d3-a456-426614174000'
const vault = { vault_id: 'personal-wiki', label: 'notes' }
const saved = {
  reference_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  vault_id: vault.vault_id,
  document_path: 'projects/review.md',
  start_line: 2,
  end_line: 12,
  source_sha256: 'a'.repeat(64),
  reason: 'Quality gate source of truth.',
}

function detail(): TaskDetail {
  return { task, context: [], activity: [], replies: [] }
}

function resumeWorkspace(): WorkspaceProjection {
  return { ...workspace, workspace: { ...workspace.workspace, id: WORKSPACE_UID } }
}

function recordedAudit(next: string, entryOverrides: Record<string, unknown> = {}): CheckpointAudit {
  const id = `CP-${'a'.repeat(64)}`
  const entryDigest = `sha256:${'b'.repeat(64)}`
  return {
    workspace_uid: WORKSPACE_UID,
    entries: [{
      locator: {
        workspace_uid: WORKSPACE_UID,
        task_id: task.id,
        date: '2026-09-07',
        ordinal: 1,
        entry_digest: entryDigest,
      },
      checkpoint_id: id,
      entry: {
        task_id: task.id,
        task: task.title,
        done: ['Shipped the drawer shell'],
        next: [next],
        blockers: [],
        ...entryOverrides,
      },
      recorded: {
        type: 'worklog.recorded',
        workspace_uid: WORKSPACE_UID,
        task_id: task.id,
        checkpoint_id: id,
        date: '2026-09-07',
        ordinal: 1,
        entry_digest: entryDigest,
        origin: 'agent-cli-v1',
      },
      state: 'active',
      revision: 0,
      transitions: [],
    }],
  }
}

function mockHost() {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  vi.mocked(requestKnowledge).mockImplementation((operation) => {
    if (operation === 'status') return Promise.resolve({ vaults: [vault], local_only: true }) as never
    if (operation === 'list-references') {
      return Promise.resolve({ binding: {}, references: [saved], local_only: true }) as never
    }
    return Promise.reject(new Error(String(operation))) as never
  })
}

function renderDrawer(audit: CheckpointAudit, fetchImpl?: (url: string, init?: RequestInit) => ReturnType<typeof jsonResponse>) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/api/v1/review/checkpoints')) return jsonResponse({ data: audit })
    if (fetchImpl) return fetchImpl(url, init)
    return jsonResponse({ data: detail() })
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <TaskDrawer onClose={vi.fn()} onNotice={vi.fn()} taskId={task.id} workspace={resumeWorkspace()} />
    </QueryClientProvider>,
  )
  return fetchMock
}

afterEach(() => {
  vi.mocked(knowledgeHostAvailable).mockReset()
  vi.mocked(requestKnowledge).mockReset()
})

test('resume reads the shared audit and lists linked documents from the host catalog', async () => {
  mockHost()
  const fetchMock = renderDrawer(recordedAudit('Keep the original 제목'))
  expect(await screen.findByText('Keep the original 제목')).toBeInTheDocument()
  expect(screen.getByText('From the latest checkpoint · 2026-09-07')).toBeInTheDocument()
  expect(await screen.findByText('review.md')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Prepare resume brief' })).toBeEnabled()
  expect(screen.getByRole('button', { name: /View all context/i })).toBeInTheDocument()
  const checkpointReads = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/api/v1/review/checkpoints'))
  expect(checkpointReads).toHaveLength(1)
  const hostOps = vi.mocked(requestKnowledge).mock.calls.map(([operation]) => operation)
  expect(hostOps).toEqual(['status', 'list-references'])
  expect(hostOps).not.toContain('read-reference')
})

test('context handoff receives the current resume facts without a second audit read', async () => {
  mockHost()
  const fetchMock = renderDrawer(recordedAudit('Keep the original 제목'))
  await screen.findByText('Keep the original 제목')
  await userEvent.click(screen.getByRole('button', { name: 'Prepare resume brief' }))
  expect(await screen.findByRole('heading', { name: 'Prepare a resume brief' })).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: 'Back to task' })).toHaveLength(1)
  expect(screen.getByText(/Includes the recorded progress snapshot/)).toBeInTheDocument()
  expect(screen.getByText(/From the latest checkpoint · 2026-09-07/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Prepare resume brief' })).not.toBeInTheDocument()
  expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/api/v1/review/checkpoints'))).toHaveLength(1)
})

test('a failed audit stays an explicit error with retry rather than an empty next step', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(false)
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).endsWith('/api/v1/review/checkpoints')) {
      return jsonResponse({ error: { code: 'unavailable', message: 'checkpoint audit failed' } }, 500)
    }
    return jsonResponse({ data: detail() })
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <TaskDrawer onClose={vi.fn()} onNotice={vi.fn()} taskId={task.id} workspace={resumeWorkspace()} />
    </QueryClientProvider>,
  )
  expect(await screen.findByRole('alert')).toHaveTextContent('checkpoint audit failed')
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  expect(screen.queryByText('No progress recorded yet.')).not.toBeInTheDocument()
  expect(screen.queryByText('Recorded progress was not available in this view.')).not.toBeInTheDocument()
})

async function openContextSubview() {
  await screen.findByText('Keep the original 제목')
  const viewAll = screen.getByRole('button', { name: /View all context/i })
  viewAll.focus()
  await userEvent.click(viewAll)
  await screen.findByRole('heading', { name: 'Prepare a resume brief' })
  return viewAll
}

test('Back to task returns focus to the control the reader left from', async () => {
  mockHost()
  renderDrawer(recordedAudit('Keep the original 제목'))
  await openContextSubview()
  await userEvent.click(screen.getByRole('button', { name: 'Back to task' }))
  const returned = await screen.findByRole('button', { name: /View all context/i })
  await waitFor(() => expect(document.activeElement).toBe(returned))
})

test('Escape leaves the subview and returns focus the same way Back does', async () => {
  mockHost()
  renderDrawer(recordedAudit('Keep the original 제목'))
  await openContextSubview()
  document.body.focus()
  await userEvent.keyboard('{Escape}')
  const returned = await screen.findByRole('button', { name: /View all context/i })
  await waitFor(() => expect(document.activeElement).toBe(returned))
  expect(screen.queryByRole('heading', { name: 'Prepare a resume brief' })).not.toBeInTheDocument()
})

test('Escape while typing in the knowledge composer keeps the subview and the draft', async () => {
  mockHost()
  renderDrawer(recordedAudit('Keep the original 제목'))
  await openContextSubview()
  const composer = await screen.findByRole('textbox', { name: 'Document search query' })
  await userEvent.type(composer, ' release gate')
  const typed = (composer as HTMLTextAreaElement).value
  expect(typed).toContain(' release gate')
  await userEvent.keyboard('{Escape}')
  expect(screen.getByRole('heading', { name: 'Prepare a resume brief' })).toBeInTheDocument()
  expect(screen.getByRole('textbox', { name: 'Document search query' })).toHaveValue(typed)
})

test('opening the drawer on Resume does not move focus off the reader', async () => {
  mockHost()
  const outside = document.createElement('button')
  document.body.append(outside)
  outside.focus()
  renderDrawer(recordedAudit('Keep the original 제목'))
  await screen.findByText('Keep the original 제목')
  expect(document.activeElement).toBe(outside)
  outside.remove()
})

test('a record whose payload names another Task says so on Resume', async () => {
  mockHost()
  renderDrawer(recordedAudit('DISPUTED next step', {
    task_id: 'T-9999',
    task: 'Some other Task',
    hand_off: { note: 'unrecognised field' },
  }))
  expect(await screen.findByText('DISPUTED next step')).toBeInTheDocument()
  const notice = screen.getByRole('status')
  expect(notice).toHaveTextContent('This record could not be presented in full.')
  expect(notice).toHaveTextContent('The recorded entry names Task T-9999 · Some other Task, not T-0001.')
  expect(notice).toHaveTextContent('hand_off')
  await userEvent.click(screen.getByText('Record details and history'))
  expect(screen.getByText(/hand_off: /)).toBeInTheDocument()
})
