import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { afterEach, expect, test, vi } from 'vitest'

import { api } from '../../api/client'
import { knowledgeHostRequestSchema } from './knowledgeHostBridge'
import type {
  KnowledgeHostRequest,
  KnowledgeReadReference,
  KnowledgeSavedReference,
} from './knowledgeTypes'
import { ReferenceHandoffPanel } from './ReferenceHandoffPanel'
import { referenceListSignature } from './TaskKnowledgePanel'
import type { ResumeProgressFacts, ResumeProgressSnapshot } from './resumeProgressContract'
import { capture, task, workspace } from '../../test/fixtures'

interface WebViewMessageEvent extends Event { data?: unknown }

const sha = 'a'.repeat(64)
const changedSha = 'b'.repeat(64)
const vault = { vault_id: 'personal-wiki', label: 'notes' }

const saved: KnowledgeSavedReference = {
  reference_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  vault_id: vault.vault_id,
  document_path: 'projects/review.md',
  start_line: 2,
  end_line: 12,
  source_sha256: sha,
  reason: 'Quality gate source of truth.',
}

const includeReview = 'Include review.md from personal-wiki'
const includeOther = 'Include other.md from personal-wiki'

const other: KnowledgeSavedReference = {
  ...saved,
  reference_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
  document_path: 'projects/other.md',
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

function liveDetail(revision = task.revision) {
  return { task: { ...task, revision }, context: [], activity: [], replies: [] }
}

function readFor(
  item: KnowledgeSavedReference,
  overrides: Partial<KnowledgeReadReference> = {},
): KnowledgeReadReference {
  return {
    schema_version: 1,
    provider: 'markdown-vault',
    vault_id: item.vault_id,
    document_path: item.document_path,
    title: 'Release review',
    source_sha256: item.source_sha256,
    expected_sha256: item.source_sha256,
    freshness: 'unchanged',
    start_line: item.start_line,
    end_line: item.end_line,
    excerpt: 'Make the quality gate measurable.\n<script>alert(1)</script>',
    excerpt_truncated: false,
    trust: 'external_reference',
    read_only: true,
    ...overrides,
  }
}

function ok(request: KnowledgeHostRequest, data: unknown) {
  return {
    type: 'workstack-knowledge-response' as const,
    schema_version: 1 as const,
    request_id: request.request_id,
    operation: request.operation,
    ok: true as const,
    data,
  }
}

function fail(request: KnowledgeHostRequest, code: string, message: string) {
  return {
    type: 'workstack-knowledge-response' as const,
    schema_version: 1 as const,
    request_id: request.request_id,
    operation: request.operation,
    ok: false as const,
    error: { code, message },
  }
}

function installTransport(handler: (request: KnowledgeHostRequest) => unknown) {
  let listener: ((event: WebViewMessageEvent) => void) | undefined
  const calls: KnowledgeHostRequest[] = []
  Object.defineProperty(window, 'chrome', {
    configurable: true,
    value: {
      webview: {
        addEventListener: (_type: string, next: typeof listener) => { listener = next },
        removeEventListener: () => { listener = undefined },
        postMessage: (message: string) => {
          const request = knowledgeHostRequestSchema.parse(JSON.parse(message))
          calls.push(request)
          void Promise.resolve().then(() => handler(request)).then((data) => {
            listener?.({ data } as WebViewMessageEvent)
          }, (error) => {
            listener?.({
              data: fail(
                request,
                error instanceof Error && error.message.length <= 64 ? 'operation_failed' : 'operation_failed',
                'The knowledge host could not complete that request.',
              ),
            } as WebViewMessageEvent)
          })
        },
      },
    },
  })
  return { calls }
}

function mockLiveTask(revision = task.revision) {
  vi.spyOn(api, 'getTask').mockResolvedValue(liveDetail(revision))
  vi.spyOn(api, 'getWorkspace').mockResolvedValue(workspace)
}

function openHandoff() {
  return userEvent.setup()
}

/**
 * Without a bound progress source the brief omits the recorded snapshot, and that omission
 * is only ever the user's explicit choice.
 */
async function acceptProgressOmission(user: ReturnType<typeof userEvent.setup>) {
  const consent = screen.queryByRole('checkbox', { name: /without a recorded progress snapshot/i })
  if (consent && !(consent as HTMLInputElement).checked) await user.click(consent)
}

afterEach(() => {
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
  Object.defineProperty(document, 'execCommand', { configurable: true, value: undefined })
})

test('lists linked references first, selects none of them, and exports nothing yet', async () => {
  const transport = installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved, other] })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  expect(await screen.findByLabelText(includeReview)).not.toBeChecked()
  expect(screen.getByLabelText(includeOther)).not.toBeChecked()
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
  expect(transport.calls.every((call) => call.operation === 'list-references')).toBe(true)
  expect(transport.calls.filter((call) => call.operation === 'list-references')).toHaveLength(1)
})

test('prepares only the selected read through the host contract, copies JSON, and downloads a path-free filename', async () => {
  const transport = installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved, other] })
    }
    if (request.operation === 'read-reference') {
      const item = request.document_path === other.document_path ? other : saved
      return ok(request, {
        binding: request.binding,
        reference: readFor(item, {
          excerpt_truncated: true,
          expected_sha256: request.expected_sha256 ?? null,
        }),
      })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  const user = openHandoff()
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  let filename = ''
  const createObjectURL = vi.fn(() => 'blob:knowledge-context')
  vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    filename = this.download
  })
  await screen.findByLabelText(includeOther)
  await user.click(screen.getByLabelText(includeOther))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByText('Unchanged source')).toBeInTheDocument()
  expect(screen.getByText((content) => content.includes('<script>alert(1)</script>'))).toBeInTheDocument()
  expect(document.querySelector('script')).toBeNull()
  expect(screen.getByLabelText(includeReview)).not.toBeChecked()
  const reads = transport.calls.filter((call) => call.operation === 'read-reference')
  expect(reads).toHaveLength(1)
  expect(reads[0]).toEqual(expect.objectContaining({
    operation: 'read-reference',
    document_path: 'projects/other.md',
    expected_sha256: sha,
    start_line: 2,
    end_line: 12,
    vault_id: vault.vault_id,
  }))
  await user.click(screen.getByRole('button', { name: 'Copy JSON' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
  const payload = JSON.parse(String(writeText.mock.calls[0]?.[0])) as {
    generated: boolean
    references: Array<{ document_path: string; excerpt_truncated: boolean; read_only: boolean; trust: string }>
    schema: string
  }
  expect(payload.schema).toBe('workstack.knowledge-context.v1')
  expect(payload.generated).toBe(false)
  expect(Object.keys(payload).sort()).toEqual(['binding', 'generated', 'references', 'schema'])
  expect(payload.references).toEqual([expect.objectContaining({
    document_path: 'projects/other.md',
    excerpt_truncated: true,
    read_only: true,
    trust: 'external_reference',
  })])
  expect(String(writeText.mock.calls[0]?.[0])).not.toMatch(/C:\\|StateRoot|vault_root/i)
  await user.click(screen.getByRole('button', { name: 'Download JSON' }))
  expect(createObjectURL).toHaveBeenCalled()
  expect(filename).toBe('workstack-knowledge-context-T-0001-r2.json')
  expect(filename).not.toContain('projects/other.md')
  expect(filename).not.toContain(workspace.workspace.id)
  await user.click(screen.getByRole('button', { name: 'Copy resume brief' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
  const brief = String(writeText.mock.calls[1]?.[0])
  expect(brief).toContain('# Resume brief')
  expect(brief).toContain(task.title)
  expect(brief).toContain(task.detail)
  expect(brief).toContain('started')
  expect(brief).toContain('untrusted evidence')
  expect(brief).toContain('projects/other.md')
  expect(brief).toContain(other.reason)
  expect(brief).toContain('## Recorded progress')
  expect(brief).toContain('does not include a recorded progress snapshot')
  expect(brief).toContain('No linked Capture sources included.')
  expect(brief).not.toMatch(/C:\\|StateRoot|vault_root/i)
  expect(await screen.findByRole('button', { name: 'Resume brief copied' })).toBeInTheDocument()
})

test('copies stored Capture catalog fields in Markdown and keeps them out of vault JSON', async () => {
  const transport = installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    if (request.operation === 'read-reference') {
      return ok(request, { binding: request.binding, reference: readFor(saved) })
    }
    throw new Error(request.operation)
  })
  const getTask = vi.spyOn(api, 'getTask').mockResolvedValue({
    task: { ...task, revision: task.revision },
    context: [{
      ...capture,
      status: 'linked',
      linked_task_ids: [task.id],
      ref: { kind: 'capture', id: capture.id },
      connections: [{ target: { kind: 'task', id: task.id }, reasons: ['capture-link'] }],
      date_precision: 'instant',
    }],
    activity: [],
    replies: [],
  })
  vi.spyOn(api, 'getWorkspace').mockResolvedValue(workspace)
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  const user = openHandoff()
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  await screen.findByLabelText(includeReview)
  getTask.mockClear()
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByText(/stored Capture catalog from that prepare/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Copy resume brief' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
  const brief = String(writeText.mock.calls[0]?.[0])
  expect(brief).toContain('## Saved Capture sources')
  expect(brief).toContain('C-0001')
  expect(brief).toContain('Release review feedback')
  expect(brief).toContain('microsoft-outlook')
  expect(brief).toContain('capture-link')
  expect(brief).not.toContain('https://outlook.office.com')
  expect(brief).not.toContain('This continues the release-quality discussion.')
  await user.click(screen.getByRole('button', { name: 'Copy JSON' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
  const json = String(writeText.mock.calls[1]?.[0])
  const payload = JSON.parse(json) as { schema: string }
  expect(payload.schema).toBe('workstack.knowledge-context.v1')
  expect(Object.keys(payload).sort()).toEqual(['binding', 'generated', 'references', 'schema'])
  expect(json).not.toContain('C-0001')
  expect(json).not.toContain('Release review feedback')
  expect(transport.calls.map((call) => call.operation).sort()).toEqual([
    'list-references',
    'list-references',
    'read-reference',
  ])
  expect(getTask).toHaveBeenCalledTimes(2)
})

test('discards A-B-A late prepares after owner switches', async () => {
  const firstRead = deferred<void>()
  let readCalls = 0
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    if (request.operation === 'read-reference') {
      readCalls += 1
      if (readCalls === 1) {
        return firstRead.promise.then(() => ok(request, {
          binding: request.binding,
          reference: readFor(saved, { excerpt: 'LATE FIRST OWNER' }),
        }))
      }
      return ok(request, { binding: request.binding, reference: readFor(saved, { excerpt: 'Current owner excerpt' }) })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  const otherTask = { ...task, id: 'T-0009', uid: '99999999-9999-4999-8999-999999999999', title: 'Other task' }
  function Harness() {
    const [current, setCurrent] = useState(task)
    return (
      <div>
        <button type="button" onClick={() => setCurrent(otherTask)}>Switch B</button>
        <button type="button" onClick={() => setCurrent(task)}>Switch A</button>
        <ReferenceHandoffPanel task={current} workspaceUid={workspace.workspace.id} />
      </div>
    )
  }
  const user = userEvent.setup()
  render(<Harness />)
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  await user.click(screen.getByRole('button', { name: 'Switch B' }))
  await user.click(screen.getByRole('button', { name: 'Switch A' }))
  firstRead.resolve()
  await waitFor(() => expect(screen.queryByText('LATE FIRST OWNER')).not.toBeInTheDocument())
  expect(await screen.findByLabelText(includeReview)).not.toBeChecked()
  expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
  expect(screen.queryByText('LATE FIRST OWNER')).not.toBeInTheDocument()
})

test('changed sources require acknowledgment that resets when the prepared payload is replaced', async () => {
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    if (request.operation === 'read-reference') {
      return ok(request, {
        binding: request.binding,
        reference: readFor(saved, {
          expected_sha256: sha,
          excerpt: 'Updated source text',
          freshness: 'changed',
          source_sha256: changedSha,
        }),
      })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  const user = openHandoff()
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByText('Source changed')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Copy JSON' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Copy resume brief' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Download JSON' })).toBeDisabled()
  await user.click(screen.getByRole('checkbox', { name: /Acknowledge changed source/i }))
  expect(screen.getByRole('button', { name: 'Copy JSON' })).toBeEnabled()
  await user.click(screen.getByLabelText(includeReview))
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByText('Source changed')).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: /Acknowledge changed source/i })).not.toBeChecked()
  expect(screen.getByRole('button', { name: 'Copy JSON' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Copy resume brief' })).toBeDisabled()
  expect(writeText).not.toHaveBeenCalled()
})

test('refuses a malformed paired read and a missing file without omitting the rest into a partial export', async () => {
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved, other] })
    }
    if (request.operation === 'read-reference' && request.document_path === saved.document_path) {
      return ok(request, {
        binding: request.binding,
        reference: readFor(saved, { document_path: 'projects/mismatch.md' }),
      })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  const user = openHandoff()
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/no longer matches/)
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()

  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved, other] })
    }
    if (request.operation === 'read-reference') {
      return fail(request, 'document_missing', 'The Markdown document could not be found.')
    }
    throw new Error(request.operation)
  })
  await user.click(screen.getByRole('button', { name: 'Try again' }))
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await user.click(screen.getByLabelText(includeOther))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/Could not read projects\/review.md/)
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
})

test('uncompared pinned reads and live Task revision mismatches refuse export', async () => {
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    if (request.operation === 'read-reference') {
      return ok(request, {
        binding: request.binding,
        reference: readFor(saved, { expected_sha256: sha, freshness: 'uncompared' }),
      })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  const user = openHandoff()
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/not compared/)
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()

  vi.spyOn(api, 'getTask').mockResolvedValue(liveDetail(9))
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    throw new Error(request.operation)
  })
  await user.click(screen.getByRole('button', { name: 'Try again' }))
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/selected Task revision/)
})

test('a Unicode payload over 32KiB fails without showing a partial snapshot', async () => {
  const hangul = '한'.repeat(6000)
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved, other] })
    }
    if (request.operation === 'read-reference') {
      const item = request.document_path === other.document_path ? other : saved
      return ok(request, { binding: request.binding, reference: readFor(item, { excerpt: hangul }) })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  const user = openHandoff()
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await user.click(screen.getByLabelText(includeOther))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/exceeds 32KiB/)
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
  expect(screen.queryByText(hangul.slice(0, 40))).not.toBeInTheDocument()
})

test('copy and download do not fire after the Task owner has already switched', async () => {
  const copyGate = deferred<void>()
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    if (request.operation === 'read-reference') {
      return ok(request, { binding: request.binding, reference: readFor(saved) })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  const writeText = vi.fn(() => copyGate.promise)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  const createObjectURL = vi.fn(() => 'blob:stale')
  vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL: vi.fn() })
  const otherTask = { ...task, id: 'T-0009', uid: '99999999-9999-4999-8999-999999999999', title: 'Other task' }
  function Harness() {
    const [current, setCurrent] = useState(task)
    return (
      <div>
        <button type="button" onClick={() => setCurrent(otherTask)}>Switch B</button>
        <ReferenceHandoffPanel task={current} workspaceUid={workspace.workspace.id} />
      </div>
    )
  }
  render(<Harness />)
  const user = openHandoff()
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  await screen.findByRole('button', { name: 'Copy JSON' })
  await user.click(screen.getByRole('button', { name: 'Copy JSON' }))
  await user.click(screen.getByRole('button', { name: 'Switch B' }))
  copyGate.resolve()
  await waitFor(() => expect(screen.queryByRole('button', { name: 'JSON copied' })).not.toBeInTheDocument())
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Download JSON' })).not.toBeInTheDocument()
  expect(createObjectURL).not.toHaveBeenCalled()
})


function installExecCommand() {
  const execCommand = vi.fn(() => true)
  Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand })
  return execCommand
}

async function prepareSaved(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
}

test('a reference removed after it was listed refuses Prepare and reads nothing', async () => {
  let listCalls = 0
  const transport = installTransport((request) => {
    if (request.operation === 'list-references') {
      listCalls += 1
      return ok(request, {
        binding: request.binding,
        local_only: true,
        references: listCalls === 1 ? [saved, other] : [other],
      })
    }
    if (request.operation === 'read-reference') {
      return ok(request, { binding: request.binding, reference: readFor(saved) })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  const user = openHandoff()
  // userEvent.setup() installs its own clipboard stub, so the double goes in after it.
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  await prepareSaved(user)

  expect(await screen.findByRole('alert')).toHaveTextContent(/no longer saved for this Task/)
  expect(listCalls).toBeGreaterThanOrEqual(2)
  expect(transport.calls.filter((call) => call.operation === 'read-reference')).toHaveLength(0)
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Download JSON' })).not.toBeInTheDocument()
  expect(writeText).not.toHaveBeenCalled()
  // The user recovers by reloading the list rather than exporting a record they never saw.
  await user.click(screen.getByRole('button', { name: 'Try again' }))
  expect(await screen.findByLabelText(includeOther)).not.toBeChecked()
  expect(screen.queryByLabelText(includeReview)).not.toBeInTheDocument()
})

test('a reference repinned to a new hash after it was listed refuses Prepare and reads nothing', async () => {
  let listCalls = 0
  const transport = installTransport((request) => {
    if (request.operation === 'list-references') {
      listCalls += 1
      return ok(request, {
        binding: request.binding,
        local_only: true,
        references: [listCalls === 1 ? saved : { ...saved, source_sha256: changedSha, start_line: 4 }],
      })
    }
    if (request.operation === 'read-reference') {
      return ok(request, { binding: request.binding, reference: readFor(saved) })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  const user = openHandoff()
  await prepareSaved(user)

  expect(await screen.findByRole('alert')).toHaveTextContent(/changed since it was listed/)
  expect(listCalls).toBeGreaterThanOrEqual(2)
  expect(transport.calls.filter((call) => call.operation === 'read-reference')).toHaveLength(0)
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
})

test('a late A-B-A list answer inside Prepare never produces a prepared snapshot', async () => {
  const staleList = deferred<void>()
  let listCalls = 0
  const transport = installTransport((request) => {
    if (request.operation === 'list-references') {
      listCalls += 1
      const answer = ok(request, { binding: request.binding, local_only: true, references: [saved] })
      return listCalls === 2 ? staleList.promise.then(() => answer) : answer
    }
    if (request.operation === 'read-reference') {
      return ok(request, { binding: request.binding, reference: readFor(saved, { excerpt: 'LATE FIRST OWNER' }) })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  const otherTask = { ...task, id: 'T-0009', uid: '99999999-9999-4999-8999-999999999999', title: 'Other task' }
  function Harness() {
    const [current, setCurrent] = useState(task)
    return (
      <div>
        <button type="button" onClick={() => setCurrent(otherTask)}>Switch B</button>
        <button type="button" onClick={() => setCurrent(task)}>Switch A</button>
        <ReferenceHandoffPanel task={current} workspaceUid={workspace.workspace.id} />
      </div>
    )
  }
  const user = userEvent.setup()
  render(<Harness />)
  await prepareSaved(user)
  await user.click(screen.getByRole('button', { name: 'Switch B' }))
  await user.click(screen.getByRole('button', { name: 'Switch A' }))
  staleList.resolve()

  await waitFor(() => expect(screen.getByLabelText(includeReview)).not.toBeChecked())
  expect(transport.calls.filter((call) => call.operation === 'read-reference')).toHaveLength(0)
  expect(screen.queryByText('LATE FIRST OWNER')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
})

test('a native clipboard rejection after an owner switch invokes no clipboard fallback', async () => {
  const copyGate = deferred<void>()
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    if (request.operation === 'read-reference') {
      return ok(request, { binding: request.binding, reference: readFor(saved) })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  const execCommand = installExecCommand()
  const writeText = vi.fn(() => copyGate.promise)
  const otherTask = { ...task, id: 'T-0009', uid: '99999999-9999-4999-8999-999999999999', title: 'Other task' }
  function Harness() {
    const [current, setCurrent] = useState(task)
    return (
      <div>
        <button type="button" onClick={() => setCurrent(otherTask)}>Switch B</button>
        <ReferenceHandoffPanel task={current} workspaceUid={workspace.workspace.id} />
      </div>
    )
  }
  render(<Harness />)
  const user = openHandoff()
  // userEvent.setup() installs its own clipboard stub, so the double goes in after it.
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  await prepareSaved(user)
  await screen.findByRole('button', { name: 'Copy JSON' })
  await user.click(screen.getByRole('button', { name: 'Copy JSON' }))
  expect(writeText).toHaveBeenCalledTimes(1)
  await user.click(screen.getByRole('button', { name: 'Switch B' }))
  copyGate.reject(new Error('permission revoked'))
  await new Promise((resolve) => { window.setTimeout(resolve, 0) })

  expect(execCommand).toHaveBeenCalledTimes(0)
  expect(document.querySelector('textarea')).toBeNull()
  expect(screen.queryByRole('button', { name: 'JSON copied' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
})

test('a native clipboard rejection for the unchanged owner still copies through the fallback', async () => {
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    if (request.operation === 'read-reference') {
      return ok(request, { binding: request.binding, reference: readFor(saved) })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  const execCommand = installExecCommand()
  const writeText = vi.fn().mockRejectedValue(new Error('permission revoked'))
  render(<ReferenceHandoffPanel task={task} workspaceUid={workspace.workspace.id} />)
  const user = openHandoff()
  // userEvent.setup() installs its own clipboard stub, so the double goes in after it.
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  await prepareSaved(user)
  await screen.findByRole('button', { name: 'Copy JSON' })
  await user.click(screen.getByRole('button', { name: 'Copy JSON' }))

  expect(await screen.findByRole('button', { name: 'JSON copied' })).toBeInTheDocument()
  expect(execCommand).toHaveBeenCalledTimes(1)
  expect(execCommand).toHaveBeenCalledWith('copy')
  expect(document.querySelector('textarea')).toBeNull()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

const checkpoint: ResumeProgressSnapshot = {
  checkpointId: 'CP-2026-09-07-1',
  recordedDate: '2026-09-07',
  ordinal: 1,
  revision: 4,
  digest: 'digest-one',
  done: ['Split the adapter out of the panel.'],
  next: ['Wire the brief into the drawer.'],
  blockers: [],
}

const readyOne: ResumeProgressFacts = { status: 'ready', snapshot: checkpoint }

const readyTwo: ResumeProgressFacts = {
  status: 'ready',
  snapshot: { ...checkpoint, digest: 'digest-two', next: ['Ship the installer.'] },
}

function installSimpleTransport() {
  return installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    if (request.operation === 'read-reference') {
      return ok(request, { binding: request.binding, reference: readFor(saved) })
    }
    throw new Error(request.operation)
  })
}

test('the prepared brief carries the frozen checkpoint record it was prepared from', async () => {
  installSimpleTransport()
  mockLiveTask()
  const user = userEvent.setup()
  const writeText = vi.fn().mockResolvedValue(undefined)
  render(
    <ReferenceHandoffPanel progress={readyOne} task={task} workspaceUid={workspace.workspace.id} />,
  )
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  await screen.findByLabelText(includeReview)
  expect(screen.getByText(/From the latest checkpoint · 2026-09-07/)).toBeInTheDocument()
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  await user.click(await screen.findByRole('button', { name: 'Copy resume brief' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
  const brief = String(writeText.mock.calls[0]?.[0])
  expect(brief).toContain('## Recorded progress')
  expect(brief).toContain('CP-2026-09-07-1')
  expect(brief).toContain('Wire the brief into the drawer.')
  expect(brief).toContain('No blockers recorded in this checkpoint.')
})

test('a checkpoint-only change invalidates a prepared brief with an unchanged Task revision', async () => {
  installSimpleTransport()
  mockLiveTask()
  const user = userEvent.setup()
  const writeText = vi.fn().mockResolvedValue(undefined)
  function Harness() {
    const [progress, setProgress] = useState<ResumeProgressFacts>(readyOne)
    return (
      <div>
        <button type="button" onClick={() => setProgress(readyTwo)}>Record progress</button>
        <ReferenceHandoffPanel progress={progress} task={task} workspaceUid={workspace.workspace.id} />
      </div>
    )
  }
  render(<Harness />)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByRole('button', { name: 'Copy resume brief' })).toBeEnabled()

  await user.click(screen.getByRole('button', { name: 'Record progress' }))
  expect(screen.getByRole('button', { name: 'Copy resume brief' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Copy JSON' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Download JSON' })).toBeDisabled()
  expect(screen.getByText(/Prepare the brief again before copying/)).toBeInTheDocument()
  expect(writeText).not.toHaveBeenCalled()

  await user.click(screen.getByRole('button', { name: 'Prepare brief again' }))
  await user.click(await screen.findByRole('button', { name: 'Copy resume brief' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
  expect(String(writeText.mock.calls[0]?.[0])).toContain('Ship the installer.')
})

test('preparation waits for the recorded progress rather than silently omitting it', async () => {
  installSimpleTransport()
  mockLiveTask()
  const user = userEvent.setup()
  render(
    <ReferenceHandoffPanel
      progress={{ status: 'loading' }}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  await screen.findByLabelText(includeReview)
  await user.click(screen.getByLabelText(includeReview))
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeDisabled()
  expect(screen.getByText(/Loading the latest recorded progress/)).toBeInTheDocument()
})

test('unavailable progress is offered as an explicit choice, not hidden', async () => {
  installSimpleTransport()
  mockLiveTask()
  const user = userEvent.setup()
  render(
    <ReferenceHandoffPanel
      progress={{ status: 'unavailable', reason: 'The checkpoint audit could not be read.' }}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  await screen.findByLabelText(includeReview)
  expect(screen.getByText(/The checkpoint audit could not be read\./)).toBeInTheDocument()
  expect(screen.getByText(/prepare the brief without a recorded progress snapshot/)).toBeInTheDocument()
  await user.click(screen.getByLabelText(includeReview))
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeDisabled()
  await user.click(screen.getByRole('checkbox', { name: /without a recorded progress snapshot/i }))
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeEnabled()
})

test('a seeded reload after a failed copy retires the dead brief instead of leaving it enabled', async () => {
  installTransport((request) => {
    if (request.operation === 'list-references') {
      return ok(request, { binding: request.binding, local_only: true, references: [saved] })
    }
    if (request.operation === 'read-reference') {
      return ok(request, { binding: request.binding, reference: readFor(saved) })
    }
    throw new Error(request.operation)
  })
  mockLiveTask()
  const user = userEvent.setup()
  const hostReload = vi.fn()
  // The seeded panel is what production mounts: the surrounding panel owns the list and
  // hands the subview a reload that re-lists through the host.
  function Harness() {
    const [references, setReferences] = useState([saved])
    return (
      <ReferenceHandoffPanel
        seed={{
          enumerated: true,
          listed: true,
          references,
          // An unchanged catalog: the reload answers with equal records, so the
          // signature — and with it the selection session — stays the same.
          reload: () => { hostReload(); setReferences([{ ...saved }]) },
          signature: referenceListSignature(references),
        }}
        task={task}
        workspaceUid={workspace.workspace.id}
      />
    )
  }
  render(<Harness />)
  // A hard clipboard failure: the async API rejects and there is no execCommand fallback.
  const writeText = vi.fn().mockRejectedValue(new Error('permission revoked'))
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  Object.defineProperty(document, 'execCommand', { configurable: true, value: undefined })

  await user.click(await screen.findByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  await user.click(await screen.findByRole('button', { name: 'Copy resume brief' }))
  const failure = await screen.findByRole('alert')
  expect(within(failure).getByText(/Clipboard access is unavailable/)).toBeInTheDocument()

  await user.click(within(failure).getByRole('button', { name: 'Try again' }))

  // The reload discards the owned payload, so the brief it was prepared from must go with
  // it. A surviving `Copy resume brief` would be a button every export handler refuses.
  await waitFor(() => expect(hostReload).toHaveBeenCalledTimes(1))
  await waitFor(() => {
    expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
  })
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeInTheDocument()
  expect(screen.getByText('0 of 1 selected')).toBeInTheDocument()
  expect(writeText).toHaveBeenCalledTimes(1)

  // Recovery: the same panel prepares again and copies once the clipboard works.
  writeText.mockResolvedValue(undefined)
  await user.click(screen.getByLabelText(includeReview))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  await user.click(await screen.findByRole('button', { name: 'Copy resume brief' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2))
  expect(String(writeText.mock.calls[1]?.[0])).toContain('# Resume brief')
  expect(await screen.findByRole('button', { name: 'Resume brief copied' })).toBeInTheDocument()
})
