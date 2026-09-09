import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, test, vi } from 'vitest'
import { api, CommitUnknownError } from '../../../api/client'
import { task } from '../../../test/fixtures'
import { GraphContextCardComposer } from './GraphContextCardComposer'
import {
  canApplyComposerResult,
  composerCloseKind,
  composerSubmitDisabled,
  graphContextDialogFocusables,
  isBlankContextDraft,
} from './GraphContextCardComposerModel'

test('blank drafts and close kinds stay local to this composer', () => {
  expect(isBlankContextDraft('  \n\t')).toBe(true)
  expect(composerCloseKind({ text: '', pending: false, unknown: false })).toBe('open')
  expect(composerCloseKind({ text: 'note', pending: true, unknown: false })).toBe('pending')
  expect(composerCloseKind({ text: 'note', pending: false, unknown: true })).toBe('unknown')
  expect(composerCloseKind({ text: 'note', pending: false, unknown: false })).toBe('dirty')
  expect(composerSubmitDisabled({ pending: false, unknown: false, text: '  ' })).toBe(true)
})

test('late tokens and replaced owners cannot apply another Task or workspace', () => {
  const tokenA = {}
  const tokenB = {}
  const owner = { taskId: 'T-0001', workspaceId: 'ws-a' }
  expect(canApplyComposerResult({
    ownerAlive: true, submittedToken: tokenA, liveToken: tokenA, submitted: owner, live: owner,
  })).toBe(true)
  expect(canApplyComposerResult({
    ownerAlive: true, submittedToken: tokenA, liveToken: tokenB, submitted: owner, live: owner,
  })).toBe(false)
  expect(canApplyComposerResult({
    ownerAlive: true, submittedToken: tokenA, liveToken: tokenA,
    submitted: owner, live: { taskId: 'T-0002', workspaceId: 'ws-a' },
  })).toBe(false)
  expect(canApplyComposerResult({
    ownerAlive: true, submittedToken: tokenA, liveToken: tokenA,
    submitted: owner, live: { taskId: 'T-0001', workspaceId: 'ws-b' },
  })).toBe(false)
  expect(canApplyComposerResult({
    ownerAlive: false, submittedToken: tokenA, liveToken: tokenA, submitted: owner, live: owner,
  })).toBe(false)
})

test('dialog focus trap query includes textarea and input, not only buttons', () => {
  const root = document.createElement('div')
  root.innerHTML = '<button type="button">Close</button><textarea></textarea><input value="x"><button disabled>Skip</button>'
  expect(graphContextDialogFocusables(root).map((el) => el.tagName)).toEqual(['BUTTON', 'TEXTAREA', 'INPUT'])
})

function renderComposer(taskId = task.id, workspaceId = 'ws-a') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const alive = { current: true }
  const node = (id: string, workspace: string) => (
    <QueryClientProvider client={client}>
      <GraphContextCardComposer
        key={`${workspace}:${id}`}
        taskId={id}
        workspaceId={workspace}
        ownerAliveRef={alive}
      />
    </QueryClientProvider>
  )
  const view = render(node(taskId, workspaceId))
  return {
    client,
    alive,
    rerenderOwner(id: string, workspace = workspaceId) { view.rerender(node(id, workspace)) },
  }
}

test('does not create on mount and refuses empty or whitespace submits', async () => {
  const create = vi.spyOn(api, 'createNote')
  renderComposer()
  expect(create).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Add context card' })).toBeDisabled()
  await userEvent.type(screen.getByRole('textbox', { name: 'Context card' }), '   ')
  expect(screen.getByRole('button', { name: 'Add context card' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Add context card' }))
  expect(create).not.toHaveBeenCalled()
})

test('creates a plain memo linked only to the selected Task and clears the saved draft', async () => {
  const create = vi.spyOn(api, 'createNote').mockResolvedValue({
    id: 'N-9', text: 'Release assumption', links: [task.id],
  })
  const view = renderComposer()
  const invalidate = vi.spyOn(view.client, 'invalidateQueries')
  await userEvent.type(screen.getByRole('textbox', { name: 'Context card' }), 'Release assumption')
  await userEvent.click(screen.getByRole('button', { name: 'Add context card' }))
  await waitFor(() => expect(create).toHaveBeenCalledExactlyOnceWith(
    'Release assumption',
    [task.id],
    expect.stringMatching(/^workstack:/),
  ))
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Context card' })).toHaveValue(''))
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['task', task.id] })
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ['workspace'] })
})

test('failed submit keeps the same key and payload and ignores a second click while pending', async () => {
  let rejectFirst!: (reason: unknown) => void
  const create = vi.spyOn(api, 'createNote')
    .mockImplementationOnce(() => new Promise((_, reject) => { rejectFirst = reject }))
    .mockRejectedValueOnce(new Error('still failing'))
  renderComposer()
  await userEvent.type(screen.getByRole('textbox', { name: 'Context card' }), 'One logical intent')
  await userEvent.click(screen.getByRole('button', { name: 'Add context card' }))
  const saving = await screen.findByRole('button', { name: 'Saving context card…' })
  expect(saving).toBeDisabled()
  await userEvent.click(saving)
  expect(create).toHaveBeenCalledTimes(1)
  await act(async () => rejectFirst(new Error('lost reply')))
  expect(await screen.findByRole('alert')).toHaveTextContent('lost reply')
  await userEvent.click(screen.getByRole('button', { name: 'Add context card' }))
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2))
  expect(create.mock.calls[1]).toEqual(create.mock.calls[0])
  expect(create.mock.calls[0]).toEqual(['One logical intent', [task.id], expect.stringMatching(/^workstack:/)])
})

test('commit_unknown freezes text, task and key for verify and blocks editing', async () => {
  const create = vi.spyOn(api, 'createNote').mockRejectedValue(
    new CommitUnknownError('The Context card may have committed. Retry the unchanged card to verify it without duplication.', new Error('lost')),
  )
  renderComposer('T-0002', 'ws-a')
  await userEvent.type(screen.getByRole('textbox', { name: 'Context card' }), '  Frozen memo  ')
  await userEvent.click(screen.getByRole('button', { name: 'Add context card' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('may have committed')
  const field = screen.getByRole('textbox', { name: 'Context card' })
  expect(field).toHaveValue('Frozen memo')
  expect(field).toHaveAttribute('readOnly')
  await userEvent.type(field, 'extra')
  expect(field).toHaveValue('Frozen memo')
  await userEvent.click(screen.getByRole('button', { name: 'Verify context card' }))
  await waitFor(() => expect(create).toHaveBeenCalledTimes(2))
  expect(create.mock.calls[1]).toEqual(create.mock.calls[0])
  expect(create.mock.calls[0]).toEqual(['Frozen memo', ['T-0002'], expect.stringMatching(/^workstack:/)])
})

test('a late success after Task or workspace switch does not clear the new draft', async () => {
  let resolveCreate!: (value: { id: string; text: string; links: string[] }) => void
  vi.spyOn(api, 'createNote').mockImplementation(() => new Promise((done) => { resolveCreate = done }))
  const view = renderComposer('T-0001', 'ws-a')
  await userEvent.type(screen.getByRole('textbox', { name: 'Context card' }), 'For T-0001')
  await userEvent.click(screen.getByRole('button', { name: 'Add context card' }))
  view.rerenderOwner('T-0002', 'ws-b')
  await userEvent.type(screen.getByRole('textbox', { name: 'Context card' }), 'Keep this')
  const invalidate = vi.spyOn(view.client, 'invalidateQueries')
  await act(async () => resolveCreate({ id: 'N-1', text: 'For T-0001', links: ['T-0001'] }))
  expect(screen.getByRole('textbox', { name: 'Context card' })).toHaveValue('Keep this')
  expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['task', 'T-0002'] })
})
