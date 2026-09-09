import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'

import { KnowledgeHostError } from './knowledgeErrors'
import { knowledgeHostAvailable, requestKnowledge } from './knowledgeHostBridge'
import { LinkedReferenceSummary } from './LinkedReferenceSummary'
import type { KnowledgeSavedReference } from './knowledgeTypes'
import { task, workspace } from '../../test/fixtures'

vi.mock('./knowledgeHostBridge', () => ({
  knowledgeHostAvailable: vi.fn(),
  requestKnowledge: vi.fn(),
}))

const vault = { vault_id: 'personal-wiki', label: 'notes' }
const sha = 'a'.repeat(64)

function savedRef(index: number): KnowledgeSavedReference {
  return {
    reference_id: `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa${index}`,
    vault_id: vault.vault_id,
    document_path: `projects/review-${index}.md`,
    start_line: 2,
    end_line: 12,
    source_sha256: sha,
    reason: `Reason ${index}.`,
  }
}

function mockHost(references: KnowledgeSavedReference[]) {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  vi.mocked(requestKnowledge).mockImplementation((operation) => {
    if (operation === 'status') return Promise.resolve({ vaults: [vault], local_only: true }) as never
    if (operation === 'list-references') {
      return Promise.resolve({ binding: {}, references, local_only: true }) as never
    }
    return Promise.reject(new Error(String(operation))) as never
  })
}

afterEach(() => {
  vi.mocked(knowledgeHostAvailable).mockReset()
  vi.mocked(requestKnowledge).mockReset()
})

test('the summary lists a bounded preview and hands the rest to the reference subview', async () => {
  mockHost([savedRef(0), savedRef(1), savedRef(2), savedRef(3)])
  const onOpenReferences = vi.fn()
  const user = userEvent.setup()
  render(
    <LinkedReferenceSummary
      onOpenReferences={onOpenReferences}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  expect(await screen.findByText('review-0.md')).toBeInTheDocument()
  expect(screen.getByText('review-2.md')).toBeInTheDocument()
  expect(screen.queryByText('review-3.md')).not.toBeInTheDocument()
  expect(screen.getByText('1 more reference is linked.')).toBeInTheDocument()
  expect(screen.getAllByText('notes · projects · lines 2–12')).toHaveLength(3)
  expect(screen.getAllByText('Linked reason')).toHaveLength(3)
  await user.click(screen.getByRole('button', { name: 'Prepare resume brief' }))
  expect(onOpenReferences).toHaveBeenCalledTimes(1)
})

test('the summary reads the binding once and never opens a document by itself', async () => {
  mockHost([savedRef(0)])
  render(
    <LinkedReferenceSummary
      onOpenReferences={vi.fn()}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  await screen.findByText('review-0.md')
  const operations = vi.mocked(requestKnowledge).mock.calls.map((call) => call[0])
  expect(operations).toEqual(['status', 'list-references'])
  expect(operations).not.toContain('read-reference')
  expect(operations).not.toContain('search-references')
})

test('an empty list says so rather than implying references exist', async () => {
  mockHost([])
  render(
    <LinkedReferenceSummary
      onOpenReferences={vi.fn()}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  expect(await screen.findByText('No references are linked to this task yet.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Prepare resume brief' })).toBeEnabled()
})

test('the browser build explains the missing host and issues no host request', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(false)
  render(
    <LinkedReferenceSummary
      onOpenReferences={vi.fn()}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  expect(await screen.findByText(/Work Stack desktop app/)).toBeInTheDocument()
  expect(requestKnowledge).not.toHaveBeenCalled()
})

test('a failed read stays recoverable instead of showing an empty list', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  vi.mocked(requestKnowledge).mockRejectedValue(
    new KnowledgeHostError('registry_unavailable', 'Local knowledge registry is unavailable.'),
  )
  render(
    <LinkedReferenceSummary
      onOpenReferences={vi.fn()}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  expect(await screen.findByRole('alert')).toHaveTextContent('Local knowledge registry is unavailable.')
  expect(screen.queryByText('No references are linked to this task yet.')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
})

test('the first paint inside the desktop app never claims the host is missing', () => {
  mockHost([savedRef(0)])
  const { container } = render(
    <LinkedReferenceSummary
      onOpenReferences={vi.fn()}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  // Asserted on the very first committed render, before the catalog effect resolves: an
  // unseeded `hostAvailable` would flash the desktop-app line inside the desktop app.
  expect(container.textContent).not.toMatch(/Work Stack desktop app/)
  expect(screen.getByText('Loading linked references…')).toBeInTheDocument()
})

test('an inherited-name host code shows the host refusal without a retry or write', async () => {
  vi.mocked(knowledgeHostAvailable).mockReturnValue(true)
  // `constructor` passes the closed lowercase code shape the host error envelope admits.
  vi.mocked(requestKnowledge).mockRejectedValue(
    new KnowledgeHostError('constructor', 'The vault index is rebuilding on this device.'),
  )
  render(
    <LinkedReferenceSummary
      onOpenReferences={vi.fn()}
      task={task}
      workspaceUid={workspace.workspace.id}
    />,
  )
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('The vault index is rebuilding on this device.')
  expect(alert.textContent).not.toContain('[object Object]')
  expect(alert.textContent).not.toContain('function')
  expect(screen.getByRole('button', { name: 'Prepare resume brief' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  const operations = vi.mocked(requestKnowledge).mock.calls.map((call) => call[0])
  expect(operations).toEqual(['status', 'list-references'])
})
