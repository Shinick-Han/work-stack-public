import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, expect, test, vi } from 'vitest'

import { KnowledgeHostError } from './knowledgeErrors'
import { knowledgeHostAvailable, requestKnowledge } from './knowledgeHostBridge'
import { LOCAL_KNOWLEDGE_COPY } from './knowledgeSession'
import { referenceListSignature, TaskKnowledgePanel } from './TaskKnowledgePanel'
import { KNOWLEDGE_SEARCH_TIMEOUT_MS } from './knowledgeTypes'
import { task, workspace } from '../../test/fixtures'

vi.mock('./knowledgeHostBridge', () => ({
  knowledgeHostAvailable: vi.fn(),
  requestKnowledge: vi.fn(),
}))

const vault = { vault_id: 'personal-wiki', label: 'notes' }
const otherVault = { vault_id: 'second-vault', label: 'archive' }
const sha = 'a'.repeat(64)
const saved = {
  reference_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  vault_id: vault.vault_id,
  document_path: 'projects/review.md',
  start_line: 2,
  end_line: 12,
  source_sha256: sha,
  reason: 'Quality gate source of truth.',
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function withAbort<T>(signal: AbortSignal | undefined, work: Promise<T>) {
  if (signal?.aborted) return Promise.reject(new KnowledgeHostError('cancelled', 'The knowledge request was cancelled.'))
  if (!signal) return work
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new KnowledgeHostError('cancelled', 'The knowledge request was cancelled.'))
    signal.addEventListener('abort', onAbort)
    work.then((value) => {
      signal.removeEventListener('abort', onAbort)
      resolve(value)
    }, (error) => {
      signal.removeEventListener('abort', onAbort)
      reject(error)
    })
  })
}

function readResult(overrides: {
  document_path?: string
  end_line?: number
  excerpt?: string
  expected_sha256?: string | null
  freshness?: 'uncompared' | 'unchanged' | 'changed'
  source_sha256?: string
  start_line?: number
  vault_id?: string
} = {}) {
  return {
    schema_version: 1 as const,
    provider: 'markdown-vault' as const,
    vault_id: overrides.vault_id ?? vault.vault_id,
    document_path: overrides.document_path ?? 'projects/review.md',
    title: 'Release review',
    source_sha256: overrides.source_sha256 ?? sha,
    expected_sha256: overrides.expected_sha256 ?? null,
    freshness: overrides.freshness ?? 'uncompared',
    start_line: overrides.start_line ?? 1,
    end_line: overrides.end_line ?? 40,
    excerpt: overrides.excerpt ?? 'Make the quality gate measurable.\n<script>alert(1)</script>',
    excerpt_truncated: false,
    trust: 'external_reference' as const,
    read_only: true as const,
  }
}

function mockAvailableHost(handlers: {
  status?: unknown
  'choose-vault'?: unknown | Promise<unknown>
  'list-references'?: (payload: { binding: { task_id: string; task_revision: number } }) => unknown | Promise<unknown>
  'read-reference'?: (payload: Record<string, unknown>) => unknown | Promise<unknown>
  'pin-reference'?: (payload: Record<string, unknown>) => unknown | Promise<unknown>
  'unpin-reference'?: (payload: Record<string, unknown>) => unknown | Promise<unknown>
  'search-references'?: (payload: Record<string, unknown>) => unknown | Promise<unknown>
}) {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  vi.mocked(requestKnowledge).mockImplementation((operation, payload = {}, _timeout, signal) => {
    const handler = handlers[operation as keyof typeof handlers]
    const result = typeof handler === 'function' ? handler(payload as never) : handler
    return withAbort(signal, Promise.resolve(result)) as never
  })
}

afterEach(() => {
  vi.mocked(knowledgeHostAvailable).mockReset()
  vi.mocked(requestKnowledge).mockReset()
})

test('explains that the browser build cannot reach the local registry', () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(false)
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  expect(LOCAL_KNOWLEDGE_COPY).toBe(
    'Links are saved on this device. They are not included in workspace sync or backups.',
  )
  expect(LOCAL_KNOWLEDGE_COPY).not.toMatch(/StateRoot|canonical/i)
  expect(screen.getByText(LOCAL_KNOWLEDGE_COPY)).toBeInTheDocument()
  expect(screen.getByText(/desktop knowledge host unavailable/i)).toBeInTheDocument()
  expect(requestKnowledge).not.toHaveBeenCalled()
})

test('lists saved metadata without echoing excerpts until Read, then links a previewed span', async () => {
  const user = userEvent.setup()
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: expect.anything(), references: [saved], local_only: true }),
    'read-reference': (payload) => ({
      binding: payload.binding,
      reference: readResult({
        start_line: payload.start_line as number,
        end_line: payload.end_line as number,
        expected_sha256: (payload.expected_sha256 as string | null | undefined) ?? null,
        freshness: payload.expected_sha256 ? 'unchanged' : 'uncompared',
      }),
    }),
    'pin-reference': (payload) => ({
      binding: payload.binding,
      local_only: true,
      reference: {
        reference_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        vault_id: payload.vault_id,
        document_path: payload.document_path,
        start_line: payload.start_line,
        end_line: payload.end_line,
        source_sha256: payload.expected_sha256,
        reason: payload.reason,
      },
    }),
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)

  expect(await screen.findByText('review.md')).toBeInTheDocument()
  expect(screen.queryByText(/script/i)).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Read' }))
  expect(await screen.findByText('Unchanged source')).toBeInTheDocument()
  expect(screen.getByText('Make the quality gate measurable.', { exact: false })).toBeInTheDocument()
  expect(screen.getByText((content) => content.includes('<script>alert(1)</script>'))).toBeInTheDocument()
  expect(document.querySelector('script')).toBeNull()

  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByText('Source not compared')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Link reason'), 'Keep the gate next to the Task.')
  await user.click(screen.getByRole('button', { name: 'Link' }))
  await waitFor(() => expect(requestKnowledge).toHaveBeenCalledWith(
    'pin-reference',
    expect.objectContaining({
      expected_sha256: sha,
      reason: 'Keep the gate next to the Task.',
      document_path: 'projects/review.md',
    }),
    expect.any(Number),
    expect.any(AbortSignal),
  ))
})

test('does not apply a late list to a Task that is no longer open', async () => {
  const firstList = deferred<{ local_only: true; references: typeof saved[] }>()
  const secondList = deferred<{ local_only: true; references: typeof saved[] }>()
  let listCalls = 0
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': (payload) => {
      listCalls += 1
      const pending = listCalls === 1 ? firstList.promise : secondList.promise
      return pending.then((value) => ({ ...value, binding: payload.binding }))
    },
  })
  function Harness() {
    const [current, setCurrent] = useState(task)
    return (
      <div>
        <button type="button" onClick={() => setCurrent({ ...task, id: 'T-0009', uid: '99999999-9999-4999-8999-999999999999', title: 'Other task' })}>
          Switch task
        </button>
        <TaskKnowledgePanel task={current} workspaceUid={workspace.workspace.id} />
      </div>
    )
  }
  const user = userEvent.setup()
  render(<Harness />)
  await waitFor(() => expect(listCalls).toBe(1))
  await user.click(screen.getByRole('button', { name: 'Switch task' }))
  await waitFor(() => expect(listCalls).toBe(2))
  firstList.resolve({ local_only: true, references: [saved] })
  secondList.resolve({
    local_only: true,
    references: [{ ...saved, document_path: 'other/task.md', reference_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc' }],
  })
  expect(await screen.findByText('task.md')).toBeInTheDocument()
  expect(screen.queryByText('review.md')).not.toBeInTheDocument()
})

test('resets a pending preview when the vault changes and reports picker cancellation', async () => {
  const preview = deferred<{ binding: object; reference: ReturnType<typeof readResult> }>()
  mockAvailableHost({
    status: { vaults: [vault, otherVault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'read-reference': () => preview.promise,
    'choose-vault': { cancelled: true },
  })
  const user = userEvent.setup()
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  expect(await screen.findByLabelText('Selected source')).toHaveValue(vault.vault_id)
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByRole('button', { name: 'Cancel' })).toBeInTheDocument()
  await user.selectOptions(screen.getByLabelText('Selected source'), otherVault.vault_id)
  preview.resolve({ binding: {}, reference: readResult() })
  await waitFor(() => expect(screen.queryByText('Release review')).not.toBeInTheDocument())
  await user.click(screen.getByRole('button', { name: 'Choose folder' }))
  expect(await screen.findByText(/closed without choosing a folder/i)).toBeInTheDocument()
})

test('cancels an in-flight preview so a late excerpt cannot appear', async () => {
  const preview = deferred<{ binding: object; reference: ReturnType<typeof readResult> }>()
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'read-reference': (_payload, signal?: AbortSignal) => withAbort(signal, preview.promise),
  })
  vi.mocked(requestKnowledge).mockImplementation((operation, payload = {}, _timeout, signal) => {
    if (operation === 'status') return withAbort(signal, Promise.resolve({ vaults: [vault], local_only: true })) as never
    if (operation === 'list-references') {
      return withAbort(signal, Promise.resolve({ binding: (payload as { binding: object }).binding, references: [], local_only: true })) as never
    }
    return withAbort(signal, preview.promise) as never
  })
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  const user = userEvent.setup()
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  await screen.findByLabelText('Document relative path')
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  await user.click(await screen.findByRole('button', { name: 'Cancel' }))
  preview.resolve({ binding: {}, reference: readResult({ excerpt: 'LATE EXCERPT' }) })
  await waitFor(() => expect(screen.queryByText('LATE EXCERPT')).not.toBeInTheDocument())
})

test('surfaces registry unavailability, changed source, and unlink', async () => {
  const user = userEvent.setup()
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  vi.mocked(requestKnowledge).mockImplementation(async (operation, payload = {}, _timeout, signal) => {
    if (signal?.aborted) throw new KnowledgeHostError('cancelled', 'The knowledge request was cancelled.')
    if (operation === 'status') throw new KnowledgeHostError('registry_unavailable', 'Local knowledge registry is unavailable.')
    void payload
    throw new KnowledgeHostError('registry_unavailable', 'Local knowledge registry is unavailable.')
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Local knowledge registry is unavailable.')

  let pinAttempts = 0
  vi.mocked(requestKnowledge).mockImplementation(async (operation, payload = {}, _timeout, signal) => {
    if (signal?.aborted) throw new KnowledgeHostError('cancelled', 'The knowledge request was cancelled.')
    if (operation === 'status') return { vaults: [vault], local_only: true } as never
    if (operation === 'list-references') {
      return { binding: (payload as { binding: object }).binding, references: [saved], local_only: true } as never
    }
    if (operation === 'read-reference') {
      return {
        binding: (payload as { binding: object }).binding,
        reference: readResult({
          start_line: 1,
          end_line: 40,
          freshness: 'changed',
          excerpt: 'Updated source text',
        }),
      } as never
    }
    if (operation === 'pin-reference') {
      pinAttempts += 1
      throw new KnowledgeHostError('source_revision_conflict', 'The Markdown source changed after the last preview.')
    }
    if (operation === 'unpin-reference') {
      return { binding: (payload as { binding: object }).binding, removed: true, local_only: true } as never
    }
    throw new Error(String(operation))
  })
  await user.click(screen.getByRole('button', { name: 'Try again' }))
  expect(await screen.findByText('review.md')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByText('Source changed')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Link reason'), 'Link anyway')
  await user.click(screen.getByRole('button', { name: 'Link' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The Markdown source changed after the last preview.')
  expect(pinAttempts).toBe(1)
  const linked = screen.getByText('Quality gate source of truth.').closest('li')
  await user.click(within(linked as HTMLElement).getByRole('button', { name: 'Unlink' }))
  await waitFor(() => expect(screen.queryByText('Quality gate source of truth.')).not.toBeInTheDocument())
})

test('does not apply a late list after workspace or revision changes', async () => {
  const firstList = deferred<{ local_only: true; references: typeof saved[] }>()
  const workspaceList = deferred<{ local_only: true; references: typeof saved[] }>()
  const revisionList = deferred<{ local_only: true; references: typeof saved[] }>()
  let listCalls = 0
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': (payload) => {
      listCalls += 1
      const pending = listCalls === 1 ? firstList.promise : listCalls === 2 ? workspaceList.promise : revisionList.promise
      return pending.then((value) => ({ ...value, binding: payload.binding }))
    },
  })
  function Harness() {
    const [current, setCurrent] = useState({ task, workspaceUid: workspace.workspace.id })
    return (
      <div>
        <button type="button" onClick={() => setCurrent({
          task,
          workspaceUid: '33333333-3333-4333-8333-333333333333',
        })}>
          Switch workspace
        </button>
        <button type="button" onClick={() => setCurrent((value) => ({
          ...value,
          task: { ...value.task, revision: value.task.revision + 1 },
        }))}>
          Switch revision
        </button>
        <TaskKnowledgePanel task={current.task} workspaceUid={current.workspaceUid} />
      </div>
    )
  }
  const user = userEvent.setup()
  render(<Harness />)
  await waitFor(() => expect(listCalls).toBe(1))
  await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
  await waitFor(() => expect(listCalls).toBe(2))
  firstList.resolve({ local_only: true, references: [saved] })
  workspaceList.resolve({
    local_only: true,
    references: [{ ...saved, document_path: 'other/workspace.md', reference_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee' }],
  })
  expect(await screen.findByText('workspace.md')).toBeInTheDocument()
  expect(screen.queryByText('review.md')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Switch revision' }))
  await waitFor(() => expect(listCalls).toBe(3))
  revisionList.resolve({
    local_only: true,
    references: [{ ...saved, document_path: 'other/revision.md', reference_id: 'ffffffff-ffff-4fff-8fff-ffffffffffff' }],
  })
  expect(await screen.findByText('revision.md')).toBeInTheDocument()
  expect(screen.queryByText('workspace.md')).not.toBeInTheDocument()
  expect(screen.queryByText('review.md')).not.toBeInTheDocument()
})

test('source revision conflict invalidates stale approval and requires an explicit refreshed Link', async () => {
  const user = userEvent.setup()
  const staleSha = 'a'.repeat(64)
  const freshSha = 'b'.repeat(64)
  let pinAttempts = 0
  let readCount = 0
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'read-reference': (payload) => {
      readCount += 1
      return {
        binding: payload.binding,
        reference: readResult({
          document_path: String(payload.document_path),
          end_line: payload.end_line as number,
          excerpt: readCount === 1 ? 'Stale excerpt' : 'Fresh excerpt',
          source_sha256: readCount === 1 ? staleSha : freshSha,
          start_line: payload.start_line as number,
        }),
      }
    },
    'pin-reference': (payload) => {
      pinAttempts += 1
      if (payload.expected_sha256 === staleSha) {
        throw new KnowledgeHostError('source_revision_conflict', 'The Markdown source changed after the last preview.')
      }
      return {
        binding: payload.binding,
        local_only: true,
        reference: {
          reference_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
          vault_id: payload.vault_id,
          document_path: payload.document_path,
          start_line: payload.start_line,
          end_line: payload.end_line,
          source_sha256: payload.expected_sha256,
          reason: payload.reason,
        },
      }
    },
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  await screen.findByLabelText('Document relative path')
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByText('Stale excerpt')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Link reason'), 'Keep the gate next to the Task.')
  await user.click(screen.getByRole('button', { name: 'Link' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The Markdown source changed after the last preview.')
  expect(screen.queryByText('Stale excerpt')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Link reason')).toHaveValue('Keep the gate next to the Task.')
  expect(screen.getByLabelText('Document relative path')).toHaveValue('projects/review.md')
  expect(screen.getByRole('button', { name: 'Link' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  expect(pinAttempts).toBe(1)
  await user.click(screen.getByRole('button', { name: 'Refresh preview' }))
  expect(await screen.findByText('Fresh excerpt')).toBeInTheDocument()
  expect(pinAttempts).toBe(1)
  await user.click(screen.getByRole('button', { name: 'Link' }))
  await waitFor(() => expect(requestKnowledge).toHaveBeenCalledWith(
    'pin-reference',
    expect.objectContaining({
      expected_sha256: freshSha,
      reason: 'Keep the gate next to the Task.',
    }),
    expect.any(Number),
    expect.any(AbortSignal),
  ))
  expect(pinAttempts).toBe(2)
})

test('ambiguous pin timeouts reconcile the list without retrying the write or clearing input', async () => {
  const user = userEvent.setup()
  let pinAttempts = 0
  let listCalls = 0
  const landed = {
    ...saved,
    document_path: 'projects/review.md',
    end_line: 40,
    reason: 'Keep this draft.',
    reference_id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
    start_line: 1,
  }
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => {
      listCalls += 1
      return { binding: {}, local_only: true, references: listCalls > 1 ? [landed] : [] }
    },
    'read-reference': (payload) => ({
      binding: payload.binding,
      reference: readResult({
        end_line: payload.end_line as number,
        start_line: payload.start_line as number,
      }),
    }),
    'pin-reference': () => {
      pinAttempts += 1
      throw new KnowledgeHostError('timeout', 'The knowledge host did not reply before the request timed out.')
    },
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  await screen.findByLabelText('Document relative path')
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  await screen.findByText('Release review')
  await user.type(screen.getByLabelText('Link reason'), 'Keep this draft.')
  await user.click(screen.getByRole('button', { name: 'Link' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('did not reply before the request timed out')
  expect(screen.getByLabelText('Link reason')).toHaveValue('Keep this draft.')
  expect(screen.getByLabelText('Document relative path')).toHaveValue('projects/review.md')
  expect(screen.getByLabelText('Include review.md from notes').closest('li')).toHaveTextContent('Keep this draft.')
  expect(pinAttempts).toBe(1)
  expect(listCalls).toBeGreaterThan(1)
  expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  const pinCalls = vi.mocked(requestKnowledge).mock.calls.filter((call) => call[0] === 'pin-reference')
  expect(pinCalls).toHaveLength(1)
})

function searchMatch(overrides: Partial<ReturnType<typeof readResult>> = {}) {
  return readResult({
    expected_sha256: sha,
    freshness: 'unchanged',
    start_line: 2,
    end_line: 12,
    ...overrides,
  })
}

test('seeds the query from the Task title and does not search until Search is clicked', async () => {
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'search-references': () => {
      throw new Error('search must be explicit')
    },
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  expect(await screen.findByLabelText('Document search query')).toHaveValue(task.title)
  expect(vi.mocked(requestKnowledge).mock.calls.map((call) => call[0])).toEqual(['status', 'list-references'])
  expect(screen.queryByLabelText('Search matches')).not.toBeInTheDocument()
})

test('search shows corpus scope, omits changed hits, and Use this result fills the link form', async () => {
  const user = userEvent.setup()
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'search-references': (payload) => ({
      binding: payload.binding,
      query: payload.query,
      corpus: { label: 'notes snapshot', document_count: 30, indexed_at: '2026-09-08T00:00:00Z' },
      matches: [
        searchMatch({ excerpt: 'Make the quality gate measurable.\n<script>alert(1)</script>' }),
        searchMatch({
          document_path: 'projects/stale.md',
          freshness: 'changed',
          title: 'Stale hit',
          excerpt: 'should not render as current',
        }),
      ],
      omitted_count: 2,
    }),
    'pin-reference': (payload) => ({
      binding: payload.binding,
      local_only: true,
      reference: {
        reference_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        vault_id: payload.vault_id,
        document_path: payload.document_path,
        start_line: payload.start_line,
        end_line: payload.end_line,
        source_sha256: payload.expected_sha256,
        reason: payload.reason,
      },
    }),
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  await screen.findByLabelText('Document search query')
  await user.click(screen.getByRole('button', { name: 'Search' }))
  expect(await screen.findByText(/notes snapshot · 30 documents · indexed 2026-09-08T00:00:00Z/)).toBeInTheDocument()
  expect(screen.getByText(/not a full index of the source/i)).toBeInTheDocument()
  expect(screen.getByText(/3 hits were omitted/i)).toBeInTheDocument()
  expect(screen.queryByText('should not render as current')).not.toBeInTheDocument()
  expect(screen.getByText((content) => content.includes('<script>alert(1)</script>'))).toBeInTheDocument()
  expect(document.querySelector('script')).toBeNull()
  await waitFor(() => expect(requestKnowledge).toHaveBeenCalledWith(
    'search-references',
    expect.objectContaining({
      query: task.title,
      vault_id: vault.vault_id,
    }),
    KNOWLEDGE_SEARCH_TIMEOUT_MS,
    expect.any(AbortSignal),
  ))
  await user.click(screen.getByRole('button', { name: 'Use this result' }))
  expect(screen.getByLabelText('Document relative path')).toHaveValue('projects/review.md')
  expect(screen.getByLabelText('Start line')).toHaveValue(2)
  expect(screen.getByLabelText('End line')).toHaveValue(12)
  await user.type(screen.getByLabelText('Link reason'), 'Keep the gate next to the Task.')
  await user.click(screen.getByRole('button', { name: 'Link' }))
  await waitFor(() => expect(requestKnowledge).toHaveBeenCalledWith(
    'pin-reference',
    expect.objectContaining({
      document_path: 'projects/review.md',
      expected_sha256: sha,
      start_line: 2,
      end_line: 12,
    }),
    expect.any(Number),
    expect.any(AbortSignal),
  ))
})

test('search unconfigured, empty, and stale query or vault replies stay actionable', async () => {
  const user = userEvent.setup()
  mockAvailableHost({
    status: { vaults: [vault, otherVault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'search-references': () => {
      throw new KnowledgeHostError('search_unconfigured', 'provider missing')
    },
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  await screen.findByLabelText('Document search query')
  await user.click(screen.getByRole('button', { name: 'Search' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/not configured on this device/)
  expect(screen.getByRole('button', { name: 'Search' })).toBeEnabled()
  expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()

  const late = deferred<{
    binding: object
    corpus: { document_count: number; indexed_at: string; label: string }
    matches: ReturnType<typeof searchMatch>[]
    omitted_count: number
    query: string
  }>()
  mockAvailableHost({
    status: { vaults: [vault, otherVault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'search-references': (payload) => late.promise.then((value) => ({ ...value, binding: payload.binding })),
  })
  await user.click(screen.getByRole('button', { name: 'Search' }))
  await user.selectOptions(screen.getByLabelText('Selected source'), otherVault.vault_id)
  late.resolve({
    binding: {},
    query: task.title,
    corpus: { label: 'stale corpus', document_count: 9, indexed_at: '2026-09-08T00:00:00Z' },
    matches: [searchMatch({ excerpt: 'LATE SEARCH' })],
    omitted_count: 0,
  })
  await waitFor(() => expect(screen.queryByText('LATE SEARCH')).not.toBeInTheDocument())
  expect(screen.queryByText('stale corpus')).not.toBeInTheDocument()

  mockAvailableHost({
    status: { vaults: [vault, otherVault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'search-references': (payload) => ({
      binding: payload.binding,
      query: payload.query,
      corpus: { label: 'archive snapshot', document_count: 0, indexed_at: '2026-09-07T12:00:00Z' },
      matches: [],
      omitted_count: 0,
    }),
  })
  await user.click(screen.getByRole('button', { name: 'Search' }))
  expect(await screen.findByText(/archive snapshot · 0 documents/)).toBeInTheDocument()
  expect(screen.getByText(/No matching documents in this corpus snapshot/)).toBeInTheDocument()
})


test('a short document clipped from the default end line can still be linked', async () => {
  const user = userEvent.setup()
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'read-reference': (payload) => ({
      binding: payload.binding,
      reference: readResult({
        document_path: String(payload.document_path),
        excerpt: 'Short note body.',
        start_line: payload.start_line as number,
        end_line: Math.min(payload.end_line as number, 8),
      }),
    }),
    'pin-reference': (payload) => ({
      binding: payload.binding,
      local_only: true,
      reference: {
        reference_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        vault_id: payload.vault_id,
        document_path: payload.document_path,
        start_line: payload.start_line,
        end_line: payload.end_line,
        source_sha256: payload.expected_sha256,
        reason: payload.reason,
      },
    }),
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  await screen.findByLabelText('Document relative path')
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByText('Short note body.')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Link reason'), 'Keep the short note with this Task.')
  expect(screen.getByRole('button', { name: 'Link' })).toBeEnabled()
  await user.type(screen.getByLabelText('End line'), '20')
  expect(screen.getByRole('button', { name: 'Link' })).toBeDisabled()
  await user.clear(screen.getByLabelText('End line'))
  expect(screen.getByRole('button', { name: 'Link' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: 'Link' }))
  await waitFor(() => expect(requestKnowledge).toHaveBeenCalledWith(
    'pin-reference',
    expect.objectContaining({
      document_path: 'projects/review.md',
      start_line: 1,
      end_line: 8,
    }),
    expect.any(Number),
    expect.any(AbortSignal),
  ))
})

test('a late preview does not land after the Task changes', async () => {
  const preview = deferred<{ binding: object; reference: ReturnType<typeof readResult> }>()
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'read-reference': () => preview.promise,
  })
  function Harness() {
    const [current, setCurrent] = useState(task)
    return (
      <div>
        <button type="button" onClick={() => setCurrent({ ...task, id: 'T-0009', uid: '99999999-9999-4999-8999-999999999999', title: 'Other task' })}>
          Switch task
        </button>
        <TaskKnowledgePanel task={current} workspaceUid={workspace.workspace.id} />
      </div>
    )
  }
  const user = userEvent.setup()
  render(<Harness />)
  await screen.findByLabelText('Document relative path')
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  await user.click(screen.getByRole('button', { name: 'Switch task' }))
  preview.resolve({
    binding: {},
    reference: readResult({
      document_path: 'projects/review.md',
      excerpt: 'LATE AFTER TASK SWITCH',
      end_line: 8,
      start_line: 1,
    }),
  })
  await waitFor(() => expect(screen.queryByText('LATE AFTER TASK SWITCH')).not.toBeInTheDocument())
  expect(screen.getByRole('button', { name: 'Link' })).toBeDisabled()
})

test('an explicit requested range is sent, and clipping still pins the effective span', async () => {
  const user = userEvent.setup()
  const pinPayloads: Array<Record<string, unknown>> = []
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'read-reference': (payload) => ({
      binding: payload.binding,
      reference: readResult({
        document_path: String(payload.document_path),
        excerpt: `Lines ${payload.start_line}-${Math.min(payload.end_line as number, 8)}`,
        start_line: payload.start_line as number,
        end_line: Math.min(payload.end_line as number, 8),
      }),
    }),
    'pin-reference': (payload) => {
      pinPayloads.push(payload)
      return {
        binding: payload.binding,
        local_only: true,
        reference: {
          reference_id: `${String(pinPayloads.length).padStart(8, 'b')}-bbbb-4bbb-8bbb-bbbbbbbbbbbb`,
          vault_id: payload.vault_id,
          document_path: payload.document_path,
          start_line: payload.start_line,
          end_line: payload.end_line,
          source_sha256: payload.expected_sha256,
          reason: payload.reason,
        },
      }
    },
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  await screen.findByLabelText('Document relative path')
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.type(screen.getByLabelText('Start line'), '2')
  await user.type(screen.getByLabelText('End line'), '12')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByText('Lines 2-8')).toBeInTheDocument()
  await waitFor(() => expect(requestKnowledge).toHaveBeenCalledWith(
    'read-reference',
    expect.objectContaining({ start_line: 2, end_line: 12 }),
    expect.any(Number),
    expect.any(AbortSignal),
  ))
  await user.type(screen.getByLabelText('Link reason'), 'Keep this explicit span.')
  await user.click(screen.getByRole('button', { name: 'Link' }))
  await waitFor(() => expect(pinPayloads).toHaveLength(1))
  expect(pinPayloads[0]).toEqual(expect.objectContaining({
    start_line: 2,
    end_line: 8,
  }))

  await user.clear(screen.getByLabelText('Start line'))
  await user.clear(screen.getByLabelText('End line'))
  await user.type(screen.getByLabelText('Start line'), '2')
  await user.type(screen.getByLabelText('End line'), '6')
  await user.type(screen.getByLabelText('Link reason'), 'Keep this explicit span.')
  expect(screen.getByRole('button', { name: 'Link' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByText('Lines 2-6')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Link' }))
  await waitFor(() => expect(pinPayloads).toHaveLength(2))
  expect(pinPayloads[1]).toEqual(expect.objectContaining({
    start_line: 2,
    end_line: 6,
  }))
})

test('the subview reads the binding once and returns to the Task on request', async () => {
  const onBack = vi.fn()
  const user = userEvent.setup()
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': (payload) => ({ binding: payload.binding, references: [saved], local_only: true }),
  })
  render(
    <TaskKnowledgePanel
      onBack={onBack}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  expect(await screen.findByRole('heading', { name: 'Prepare a resume brief' })).toBeInTheDocument()
  expect(await screen.findByLabelText('Include review.md from notes')).not.toBeChecked()
  expect(vi.mocked(requestKnowledge).mock.calls.map((call) => call[0])).toEqual(['status', 'list-references'])
  await user.click(screen.getByRole('button', { name: 'Back to task' }))
  expect(onBack).toHaveBeenCalledTimes(1)
})

test('the panel names only the connector that works and offers no other connection', async () => {
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  expect(await screen.findByText('Local Markdown')).toBeInTheDocument()
  expect(screen.getByText(/An Obsidian vault is one such folder/)).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Knowledge sources' })).toBeInTheDocument()
  expect(screen.queryByText(/Notion/i)).not.toBeInTheDocument()
  const buttons = screen.getAllByRole('button').map((button) => button.textContent ?? '')
  expect(buttons.some((label) => /connect/i.test(label))).toBe(false)
})

test('opening the reference subview issues no search and no document read', async () => {
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [saved], local_only: true }),
    'search-references': () => { throw new Error('search must be explicit') },
    'read-reference': () => { throw new Error('reads must be explicit') },
  })
  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  await screen.findByLabelText('Include review.md from notes')
  const operations = vi.mocked(requestKnowledge).mock.calls.map((call) => call[0])
  expect(operations).not.toContain('search-references')
  expect(operations).not.toContain('read-reference')
  expect(operations.filter((operation) => operation === 'list-references')).toHaveLength(1)
})

test('a reason carrying the separators cannot forge another record into the list signature', () => {
  const second = {
    reference_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    vault_id: otherVault.vault_id,
    document_path: 'projects/other.md',
    start_line: 3,
    end_line: 9,
    source_sha256: 'b'.repeat(64),
    reason: 'Second record.',
  }
  // `reason` is free user text. Spelled with the field and record separators a joined
  // signature would use, one reference imitates a two-reference list exactly.
  const forged = {
    ...saved,
    reason: [
      `${saved.reason}\u0002${second.reference_id}`,
      second.vault_id,
      second.document_path,
      String(second.start_line),
      String(second.end_line),
      second.source_sha256,
      second.reason,
    ].join('\u0001'),
  }
  const joined = (references: readonly (typeof saved)[]) => references
    .map((item) => [
      item.reference_id,
      item.vault_id,
      item.document_path,
      item.start_line,
      item.end_line,
      item.source_sha256,
      item.reason,
    ].join('\u0001'))
    .join('\u0002')

  // The separator-joined form these two lists would have shared, and did not survive.
  expect(joined([forged])).toBe(joined([saved, second]))
  expect(referenceListSignature([forged])).not.toBe(referenceListSignature([saved, second]))
  // Unequal lists must sign differently; equal ones must still sign identically, or the
  // selection session would restart on every render.
  expect(referenceListSignature([saved, second])).toBe(referenceListSignature([{ ...saved }, { ...second }]))
})

function pinnedFrom(payload: Record<string, unknown>, index: number) {
  return {
    binding: payload.binding,
    local_only: true,
    reference: {
      reference_id: `${String(index).padStart(8, 'c')}-cccc-4ccc-8ccc-cccccccccccc`,
      vault_id: payload.vault_id,
      document_path: payload.document_path,
      start_line: payload.start_line,
      end_line: payload.end_line,
      source_sha256: payload.expected_sha256,
      reason: payload.reason,
    },
  }
}

function RevisionHarness() {
  const [current, setCurrent] = useState(task)
  return (
    <div>
      <button type="button" onClick={() => setCurrent((value) => ({ ...value, revision: value.revision + 1 }))}>
        Bump revision
      </button>
      <TaskKnowledgePanel task={current} workspaceUid={workspace.workspace.id} />
    </div>
  )
}

test('a settled preview cannot link after a revision-only Task change', async () => {
  const user = userEvent.setup()
  const pinPayloads: Array<Record<string, unknown>> = []
  const listedRevisions: number[] = []
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': (payload) => {
      listedRevisions.push(payload.binding.task_revision)
      return { binding: {}, references: [], local_only: true }
    },
    'read-reference': (payload) => ({
      binding: payload.binding,
      reference: readResult({
        document_path: String(payload.document_path),
        excerpt: 'Evidence read under revision 2.',
        start_line: payload.start_line as number,
        end_line: Math.min(payload.end_line as number, 8),
      }),
    }),
    'pin-reference': (payload) => {
      pinPayloads.push(payload)
      return pinnedFrom(payload, pinPayloads.length)
    },
  })
  render(<RevisionHarness />)
  await screen.findByLabelText('Document relative path')
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByText('Evidence read under revision 2.')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Link reason'), 'Keep the evidence with this Task.')
  expect(screen.getByRole('button', { name: 'Link' })).toBeEnabled()

  await user.click(screen.getByRole('button', { name: 'Bump revision' }))
  await waitFor(() => expect(listedRevisions).toEqual([task.revision, task.revision + 1]))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Link' })).toBeDisabled())
  expect(screen.queryByText('Evidence read under revision 2.')).not.toBeInTheDocument()
  expect(pinPayloads).toEqual([])

  await user.click(screen.getByRole('button', { name: 'Preview' }))
  expect(await screen.findByText('Evidence read under revision 2.')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Link' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'Link' }))
  await waitFor(() => expect(pinPayloads).toHaveLength(1))
  expect(pinPayloads[0]).toMatchObject({
    binding: expect.objectContaining({ task_revision: task.revision + 1 }),
    document_path: 'projects/review.md',
    start_line: 1,
    end_line: 8,
    expected_sha256: sha,
  })
})

test('a preview in flight across a revision-only Task change never lands', async () => {
  const preview = deferred<{ binding: object; reference: ReturnType<typeof readResult> }>()
  const pinPayloads: Array<Record<string, unknown>> = []
  mockAvailableHost({
    status: { vaults: [vault], local_only: true },
    'list-references': () => ({ binding: {}, references: [], local_only: true }),
    'read-reference': () => preview.promise,
    'pin-reference': (payload) => {
      pinPayloads.push(payload)
      return pinnedFrom(payload, pinPayloads.length)
    },
  })
  const user = userEvent.setup()
  render(<RevisionHarness />)
  await screen.findByLabelText('Document relative path')
  await user.type(screen.getByLabelText('Document relative path'), 'projects/review.md')
  await user.type(screen.getByLabelText('Link reason'), 'Keep the evidence with this Task.')
  await user.click(screen.getByRole('button', { name: 'Preview' }))
  await user.click(screen.getByRole('button', { name: 'Bump revision' }))
  preview.resolve({
    binding: {},
    reference: readResult({
      document_path: 'projects/review.md',
      excerpt: 'LATE AFTER REVISION BUMP',
      end_line: 8,
      start_line: 1,
    }),
  })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Link' })).toBeDisabled())
  expect(screen.queryByText('LATE AFTER REVISION BUMP')).not.toBeInTheDocument()
  expect(pinPayloads).toEqual([])
})
