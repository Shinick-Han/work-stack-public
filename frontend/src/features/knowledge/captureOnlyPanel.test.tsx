import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'

import { api } from '../../api/client'
import { knowledgeHostAvailable, requestKnowledge } from './knowledgeHostBridge'
import { NO_SELECTED_REFERENCE_COPY } from './referenceBrief'
import { ReferenceHandoffPanel, UNLISTED_REFERENCES_COPY } from './ReferenceHandoffPanel'
import { referenceListSignature, TaskKnowledgePanel } from './TaskKnowledgePanel'
import { task, workspace } from '../../test/fixtures'

vi.mock('./knowledgeHostBridge', () => ({
  knowledgeHostAvailable: vi.fn(),
  requestKnowledge: vi.fn(),
}))

const sha = 'a'.repeat(64)
const vault = { vault_id: 'personal-wiki', label: 'notes' }

const saved = {
  reference_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  vault_id: vault.vault_id,
  document_path: 'projects/review.md',
  start_line: 2,
  end_line: 12,
  source_sha256: sha,
  reason: 'Quality gate source of truth.',
}

function captureRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'C-0001',
    status: 'linked',
    source: {
      provider: 'microsoft-outlook',
      resource_type: 'mail.message',
      display_title: 'Release review feedback',
      web_url: 'https://outlook.office.com/mail/deeplink/read/demo',
    },
    normalized: { summary: 'Secret summary that must not copy.' },
    ref: { kind: 'capture', id: 'C-0001' },
    connections: [{ target: { kind: 'task', id: task.id }, reasons: ['capture-link'] }],
    ...overrides,
  }
}

function mockLiveTask(context: unknown) {
  vi.spyOn(api, 'getTask').mockResolvedValue({
    task,
    context,
    activity: [],
    replies: [],
  } as never)
  vi.spyOn(api, 'getWorkspace').mockResolvedValue(workspace)
}

/**
 * The live Task read held open, so a Capture-only preparation can be observed mid-flight.
 * `release` resolves it exactly once, from the test.
 */
function deferredLiveTask(context: unknown) {
  let release = () => {}
  const gate = new Promise((resolve) => {
    release = () => resolve({ task, context, activity: [], replies: [] })
  })
  vi.spyOn(api, 'getTask').mockReturnValue(gate as never)
  vi.spyOn(api, 'getWorkspace').mockResolvedValue(workspace)
  return release
}

/** One macrotask turn, which drains the microtasks a late preparation resolves through. */
async function flushPending() {
  await act(async () => { await new Promise((resolve) => { setTimeout(resolve, 0) }) })
}

/** An empty list with one signature, so only the settled-list fact changes between renders. */
function emptySeed(listed: boolean) {
  return { enumerated: true, listed, references: [], reload: () => {}, signature: referenceListSignature([]) }
}

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  return writeText
}

/** The default progress source is unbound, so omitting it stays an explicit choice. */
async function acceptProgressOmission(user: ReturnType<typeof userEvent.setup>) {
  const consent = screen.queryByRole('checkbox', { name: /without a recorded progress snapshot/i })
  if (consent && !(consent as HTMLInputElement).checked) await user.click(consent)
}

afterEach(() => {
  vi.mocked(knowledgeHostAvailable).mockReset()
  vi.mocked(requestKnowledge).mockReset()
  vi.restoreAllMocks()
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
})

test('a client with no knowledge host still prepares and copies a Capture-only brief', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(false)
  mockLiveTask([captureRow()])
  const user = userEvent.setup()
  // After `setup`, which installs its own clipboard stub.
  const writeText = stubClipboard()

  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)

  // The existing host explanation stays; the handoff panel is no longer hidden behind it.
  expect(screen.getByText(/desktop knowledge host unavailable/i)).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'References for this task' })).toBeInTheDocument()
  // Local link/search controls stay host-gated.
  expect(screen.queryByText('Find or link a document')).not.toBeInTheDocument()

  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  const copy = await screen.findByRole('button', { name: 'Copy resume brief' })

  // No JSON form exists for this shape, so its controls are absent rather than disabled.
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Download JSON' })).not.toBeInTheDocument()
  expect(screen.queryByText('Other formats')).not.toBeInTheDocument()
  expect(screen.getByText(/1 saved Capture source is included/)).toBeInTheDocument()

  await user.click(copy)
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
  const copied = writeText.mock.calls[0]?.[0] as string
  expect(copied).toContain('# Resume brief')
  expect(copied).toContain('## Saved Capture sources')
  expect(copied).toContain('C-0001')
  expect(copied).toContain(NO_SELECTED_REFERENCE_COPY)
  expect(copied).not.toContain('Secret summary that must not copy')
  expect(copied).not.toContain('outlook.office.com')
  // Nothing was asked of the knowledge host on this path.
  expect(requestKnowledge).not.toHaveBeenCalled()
})

test('a failed reference list is not an empty one, in the button or in the action', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  vi.mocked(requestKnowledge).mockRejectedValue(new Error('registry down'))
  mockLiveTask([captureRow()])
  const user = userEvent.setup()

  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)

  const prepare = await screen.findByRole('button', { name: 'Prepare brief' })
  await acceptProgressOmission(user)
  expect(prepare).toBeDisabled()

  // The hook action carries the same guard, so a click that reaches it does nothing.
  await user.click(prepare)
  expect(api.getTask).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
})

test('a settled empty list prepares a Capture-only brief and a selection restores the JSON form', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  let references: (typeof saved)[] = []
  vi.mocked(requestKnowledge).mockImplementation(((operation: string, payload: { binding?: unknown } = {}) => {
    if (operation === 'status') return Promise.resolve({ vaults: [vault], local_only: true })
    if (operation === 'list-references') {
      return Promise.resolve({ binding: payload.binding, references, local_only: true })
    }
    if (operation === 'read-reference') {
      return Promise.resolve({
        binding: payload.binding,
        reference: {
          schema_version: 1,
          provider: 'markdown-vault',
          vault_id: saved.vault_id,
          document_path: saved.document_path,
          title: 'Release review',
          source_sha256: sha,
          expected_sha256: sha,
          freshness: 'unchanged',
          start_line: saved.start_line,
          end_line: saved.end_line,
          excerpt: 'Make the quality gate measurable.',
          excerpt_truncated: false,
          trust: 'external_reference',
          read_only: true,
        },
      })
    }
    return Promise.reject(new Error(operation))
  }) as never)
  mockLiveTask([captureRow()])
  const user = userEvent.setup()

  const { rerender } = render(
    <TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />,
  )
  const prepare = await screen.findByRole('button', { name: 'Prepare brief' })
  await acceptProgressOmission(user)
  await user.click(prepare)
  expect(await screen.findByRole('button', { name: 'Copy resume brief' })).toBeEnabled()
  expect(screen.queryByRole('button', { name: 'Copy JSON' })).not.toBeInTheDocument()

  // The same panel, now with a reference selected, is the unchanged vault export.
  references = [saved]
  vi.spyOn(api, 'getTask').mockResolvedValue({
    task: { ...task, revision: task.revision + 1 },
    context: [captureRow()],
    activity: [],
    replies: [],
  } as never)
  rerender(
    <TaskKnowledgePanel
      task={{ ...task, revision: task.revision + 1 }}
      workspaceUid={workspace.workspace.id}
    />,
  )
  await user.click(await screen.findByLabelText(/^Include review\.md/))
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByRole('button', { name: 'Copy JSON' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Download JSON' })).toBeInTheDocument()
})

test('a Capture-only brief prepared for one Task revision is discarded when the revision moves', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(false)
  mockLiveTask([captureRow()])
  const user = userEvent.setup()

  const { rerender } = render(
    <TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />,
  )
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByRole('button', { name: 'Copy resume brief' })).toBeInTheDocument()

  rerender(
    <TaskKnowledgePanel
      task={{ ...task, revision: task.revision + 1 }}
      workspaceUid={workspace.workspace.id}
    />,
  )
  await waitFor(() => {
    expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
  })
})

test('a settled and an unsettled empty list sign identically, so the fact is tracked apart from the session key', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  mockLiveTask([captureRow()])
  const user = userEvent.setup()
  const seedFor = (listed: boolean) => ({
    // A host that answered once: this fixture is about the settled fact, not the host.
    enumerated: listed,
    listed,
    references: [],
    reload: () => {},
    // The identical signature is the point: an unread empty list and a settled empty one
    // are the same records, so the selection session never restarts between them.
    signature: referenceListSignature([]),
  })

  const { rerender } = render(
    <ReferenceHandoffPanel seed={seedFor(false)} task={task} workspaceUid={workspace.workspace.id} />,
  )
  await acceptProgressOmission(user)
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeDisabled()

  rerender(
    <ReferenceHandoffPanel seed={seedFor(true)} task={task} workspaceUid={workspace.workspace.id} />,
  )
  await waitFor(() => expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(await screen.findByRole('button', { name: 'Copy resume brief' })).toBeInTheDocument()

  // Losing the settled list retires the brief that stood on it, same signature or not.
  rerender(
    <ReferenceHandoffPanel seed={seedFor(false)} task={task} workspaceUid={workspace.workspace.id} />,
  )
  await waitFor(() => {
    expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
  })
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeDisabled()
})

test('an unreadable stored Capture context refuses instead of preparing an empty-looking brief', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(false)
  // Two rows claiming the same Capture id: identity the R30 projector refuses.
  mockLiveTask([captureRow(), captureRow()])
  const user = userEvent.setup()

  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))

  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent(/stored Capture context for this Task could not be read/)
  expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
  // A refused preparation stays retryable; it is not a list failure.
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeEnabled()
})

test('a client with no host never presents its unlisted references as none linked', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(false)
  mockLiveTask([captureRow()])

  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)

  // The existing unavailable-host explanation is what the screen says about the host.
  expect(screen.getByText(/desktop knowledge host unavailable/i)).toBeInTheDocument()
  // Host absence is not evidence about the Task's saved references, and the link tools
  // this copy points at are not rendered on this client at all.
  expect(screen.queryByText(/No references are linked to this task yet/)).not.toBeInTheDocument()
  expect(screen.queryByText(/Use Find or link a document below/)).not.toBeInTheDocument()
  expect(screen.getByText(UNLISTED_REFERENCES_COPY)).toBeInTheDocument()
  // The Capture-only preparation this client exists for stays available.
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeInTheDocument()
})

test('an empty list the host really enumerated keeps its supported empty wording', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  vi.mocked(requestKnowledge).mockImplementation(((operation: string, payload: { binding?: unknown } = {}) => {
    if (operation === 'status') return Promise.resolve({ vaults: [vault], local_only: true })
    if (operation === 'list-references') {
      return Promise.resolve({ binding: payload.binding, references: [], local_only: true })
    }
    return Promise.reject(new Error(operation))
  }) as never)
  mockLiveTask([captureRow()])

  render(<TaskKnowledgePanel task={task} workspaceUid={workspace.workspace.id} />)

  expect(await screen.findByText(/No references are linked to this task yet/)).toBeInTheDocument()
  expect(screen.queryByText(UNLISTED_REFERENCES_COPY)).not.toBeInTheDocument()
})

test('losing the settled list mid-preparation leaves no brief for the late resolution to hand back', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  const user = userEvent.setup()
  const writeText = stubClipboard()
  const release = deferredLiveTask([captureRow()])

  const { rerender } = render(
    <ReferenceHandoffPanel seed={emptySeed(true)} task={task} workspaceUid={workspace.workspace.id} />,
  )
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  expect(api.getTask).toHaveBeenCalledTimes(1)

  // The same records under the same signature: only the settled-list fact is lost, which
  // is what a concurrent host failure does to this panel while the read is still open.
  rerender(
    <ReferenceHandoffPanel seed={emptySeed(false)} task={task} workspaceUid={workspace.workspace.id} />,
  )
  release()
  await flushPending()

  expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
  expect(writeText).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeDisabled()

  // An explicit successful list, and a new preparation, work normally.
  mockLiveTask([captureRow()])
  rerender(
    <ReferenceHandoffPanel seed={emptySeed(true)} task={task} workspaceUid={workspace.workspace.id} />,
  )
  await waitFor(() => expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))
  await user.click(await screen.findByRole('button', { name: 'Copy resume brief' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
  expect(writeText.mock.calls[0]?.[0] as string).toContain(NO_SELECTED_REFERENCE_COPY)
})

test('a settled list lost and regained before the late resolution does not resurrect that work', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  const user = userEvent.setup()
  const writeText = stubClipboard()
  const release = deferredLiveTask([captureRow()])

  const { rerender } = render(
    <ReferenceHandoffPanel seed={emptySeed(true)} task={task} workspaceUid={workspace.workspace.id} />,
  )
  await acceptProgressOmission(user)
  await user.click(screen.getByRole('button', { name: 'Prepare brief' }))

  // True, false and true again under one signature, all before the first read resolves.
  rerender(
    <ReferenceHandoffPanel seed={emptySeed(false)} task={task} workspaceUid={workspace.workspace.id} />,
  )
  rerender(
    <ReferenceHandoffPanel seed={emptySeed(true)} task={task} workspaceUid={workspace.workspace.id} />,
  )
  release()
  await flushPending()

  // The regained fact re-enables preparing; it does not adopt the revoked flight's result.
  expect(screen.queryByRole('button', { name: 'Copy resume brief' })).not.toBeInTheDocument()
  expect(writeText).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Prepare brief' })).toBeEnabled()
})
