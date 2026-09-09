import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import {
  KnowledgeIssueError,
  KnowledgeRequestDialog,
  type KnowledgeRequestDialogProps,
} from './KnowledgeRequestDialog'
import {
  KNOWLEDGE_REQUEST_SCHEMA,
  type KnowledgeCorpusOption,
  type KnowledgeRequestDraft,
} from './knowledgeRequestDraft'

const WORKSPACE = '66666666-6666-4666-8666-666666666666'
const TASK = {
  id: 'T-0033',
  revision: 2,
  title: 'Confirm the rollback owner',
  uid: '77777777-7777-4777-8777-777777777777',
}
const OTHER_TASK = {
  id: 'T-0099',
  revision: 4,
  title: 'Draft the launch note',
  uid: '88888888-8888-4888-8888-888888888888',
}
const REQUEST_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const OPEN_AT = Date.parse('2026-09-08T09:00:00.000Z')
const WINDOW_MS = 300_000

const OPTIONS: KnowledgeCorpusOption[] = [
  { alias: 'nas-team-share', description: 'Shared operational notes.', label: 'Team share' },
  { alias: 'notion-product', label: 'Product notes' },
]

const QUERY = 'rollback verification owner'

afterEach(() => {
  vi.useRealTimers()
  window.localStorage.clear()
})

function envelopeFor(draft: KnowledgeRequestDraft, at: number, overrides: Record<string, unknown> = {}) {
  return {
    binding: draft.binding,
    corpus_refs: draft.corpus_refs,
    expires_at: new Date(at + WINDOW_MS).toISOString(),
    purpose: draft.purpose,
    query: draft.query,
    request_id: REQUEST_ID,
    requested_at: new Date(at).toISOString(),
    result_limit: draft.result_limit,
    schema: KNOWLEDGE_REQUEST_SCHEMA,
    ...overrides,
  }
}

interface Harness {
  clock: { ms: number }
  copyText: ReturnType<typeof vi.fn>
  onClose: ReturnType<typeof vi.fn>
  onIssue: ReturnType<typeof vi.fn>
  rerender: (next: Partial<KnowledgeRequestDialogProps>) => void
  unmount: () => void
}

function renderDialog(overrides: Partial<KnowledgeRequestDialogProps> = {}): Harness {
  const clock = { ms: OPEN_AT }
  const onIssue =
    overrides.onIssue ??
    vi.fn(async (draft: KnowledgeRequestDraft) => envelopeFor(draft, clock.ms))
  const copyText = overrides.copyText ?? vi.fn(async () => {})
  const onClose = overrides.onClose ?? vi.fn()
  const props: KnowledgeRequestDialogProps = {
    corpusOptions: OPTIONS,
    now: () => clock.ms,
    onClose,
    onIssue,
    open: true,
    task: TASK,
    workspaceUid: WORKSPACE,
    ...overrides,
    copyText,
  }
  const view = render(<KnowledgeRequestDialog {...props} />)
  return {
    clock,
    copyText: copyText as ReturnType<typeof vi.fn>,
    onClose: onClose as ReturnType<typeof vi.fn>,
    onIssue: onIssue as ReturnType<typeof vi.fn>,
    rerender: (next) => view.rerender(<KnowledgeRequestDialog {...props} {...next} />),
    unmount: view.unmount,
  }
}

function queryField() {
  return screen.getByLabelText('Question') as HTMLTextAreaElement
}

function generateButton() {
  return screen.getByRole('button', { name: /Generate request|Generating/ })
}

function copyButton() {
  return screen.getByRole('button', { name: /Copy request|Copying/ })
}

function receipt() {
  return screen.queryByRole('region', { name: 'Issued request' })
}

/** Writes the question and picks one corpus without touching any other control. */
function compose(query = QUERY, corpus = /Team share/) {
  fireEvent.change(queryField(), { target: { value: query } })
  fireEvent.click(screen.getByRole('checkbox', { name: corpus }))
}

/**
 * One `onIssue` call the test releases by hand, so a reply can arrive after the editor
 * has already moved on.
 */
function heldIssue() {
  let release: (value: unknown) => void = () => {}
  const held = new Promise<unknown>((resolve) => { release = resolve })
  const drafts: KnowledgeRequestDraft[] = []
  const onIssue = vi.fn((draft: KnowledgeRequestDraft) => {
    drafts.push(draft)
    return held
  })
  return {
    onIssue,
    releaseWith: (at = OPEN_AT, overrides: Record<string, unknown> = {}) =>
      release(envelopeFor(drafts[0], at, overrides)),
  }
}

/** Drains the microtask queue so a settled `onIssue` promise reaches the component. */
async function flush() {
  await act(async () => {
    for (let turn = 0; turn < 5; turn += 1) await Promise.resolve()
  })
}

test('the reviewed question and scope are what is issued, and only then can be copied', async () => {
  const user = userEvent.setup()
  const harness = renderDialog()

  // The Task title prefills the editable query; nothing is issued and nothing is
  // copyable before the user asks for it.
  expect(queryField()).toHaveValue(TASK.title)
  expect(receipt()).toBeNull()
  expect(screen.queryByRole('button', { name: /Copy request/ })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Run connected search' })).toBeNull()

  await user.clear(queryField())
  await user.type(queryField(), QUERY)
  await user.click(screen.getByRole('checkbox', { name: /Team share/ }))

  const review = screen.getByRole('region', { name: 'Request review' })
  expect(review).toHaveTextContent(QUERY)
  expect(review).toHaveTextContent('Team share')
  expect(harness.onIssue).not.toHaveBeenCalled()

  await user.click(generateButton())

  expect(harness.onIssue).toHaveBeenCalledTimes(1)
  expect(harness.onIssue).toHaveBeenCalledWith({
    binding: {
      task_id: 'T-0033',
      task_revision: 2,
      task_uid: TASK.uid,
      workspace_uid: WORKSPACE,
    },
    corpus_refs: ['nas-team-share'],
    purpose: 'find_context',
    query: QUERY,
    result_limit: 5,
  })

  const panel = await screen.findByRole('region', { name: 'Issued request' })
  expect(panel).toHaveTextContent(REQUEST_ID)
  expect(panel).toHaveTextContent(QUERY)

  await user.click(copyButton())
  await waitFor(() => expect(harness.copyText).toHaveBeenCalledTimes(1))
  const copied = JSON.parse(harness.copyText.mock.calls[0][0] as string) as Record<string, unknown>
  expect(copied).toEqual(await harness.onIssue.mock.results[0].value)
  expect(screen.getByText('Copied to the clipboard.')).toBeInTheDocument()

  // The receipt is readable on screen and nowhere else.
  expect(window.localStorage.length).toBe(0)
  expect(window.location.href).not.toContain('rollback')
})

test('a request refuses before issue when the question or the scope is unusable', async () => {
  const user = userEvent.setup()
  const harness = renderDialog({ task: null })

  await user.click(generateButton())
  expect(harness.onIssue).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent('Write a question in plain text')

  await user.type(queryField(), QUERY)
  await user.click(generateButton())
  expect(harness.onIssue).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent('Select at least one corpus')

  await user.click(screen.getByRole('checkbox', { name: /Team share/ }))
  fireEvent.change(screen.getByLabelText(/Results to ask for/), { target: { value: '11' } })
  await user.click(generateButton())
  expect(harness.onIssue).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent('between 1 and 10 results')

  fireEvent.change(screen.getByLabelText(/Results to ask for/), { target: { value: '3' } })
  await user.click(generateButton())
  await screen.findByRole('region', { name: 'Issued request' })
  // No Task is open, so the workspace-only binding is a complete request.
  expect(harness.onIssue.mock.calls[0][0].binding).toEqual({ workspace_uid: WORKSPACE })
})

test('a workspace with no granted corpus explains itself and offers nothing to generate', () => {
  const harness = renderDialog({ corpusOptions: [] })
  expect(screen.getByText(/No corpus is available to this workspace yet/)).toBeInTheDocument()
  expect(generateButton()).toBeDisabled()
  expect(harness.onIssue).not.toHaveBeenCalled()
})

test('an issuer refusal the parent actually read leaves an authored error and no receipt', async () => {
  const onIssue = vi.fn(async () => {
    throw new KnowledgeIssueError('refused', '500 from https://issuer.internal/knowledge?token=hunter2')
  })
  renderDialog({ onIssue })
  compose()
  fireEvent.click(generateButton())
  await flush()

  expect(receipt()).toBeNull()
  const alert = screen.getByRole('alert')
  expect(alert).toHaveTextContent('The request was not issued')
  expect(alert.textContent).not.toContain('hunter2')
  expect(alert.textContent).not.toContain('issuer.internal')
  // A refusal is the one outcome that *is* known, so it is not hedged.
  expect(screen.queryByText(/unconfirmed rather than refused/)).toBeNull()
})

test('a rejection the parent could not classify is unconfirmed, never a refusal', async () => {
  const onIssue = vi.fn(async () => {
    throw new Error('500 from https://issuer.internal/knowledge?token=hunter2')
  })
  renderDialog({ onIssue })
  compose()
  fireEvent.click(generateButton())
  await flush()

  expect(receipt()).toBeNull()
  const alert = screen.getByRole('alert')
  expect(alert).toHaveTextContent('whether this request was issued is not known')
  expect(screen.getByText(/unconfirmed rather than refused/)).toBeInTheDocument()
  expect(screen.queryByText(/The request was not issued/)).toBeNull()
  expect(alert.textContent).not.toContain('hunter2')
  expect(alert.textContent).not.toContain('issuer.internal')
  // The same question is still on screen, so generating again is a retry of this attempt.
  expect(generateButton()).toBeEnabled()
  expect(queryField()).toHaveValue(QUERY)
})

test('an answer about another connection or revision is unknown, not a refusal', async () => {
  const onIssue = vi.fn(async () => {
    throw new KnowledgeIssueError('stale')
  })
  renderDialog({ onIssue })
  compose()
  fireEvent.click(generateButton())
  await flush()

  expect(receipt()).toBeNull()
  expect(screen.getByRole('alert')).toHaveTextContent('was about a different connection')
  expect(screen.getByText(/unconfirmed rather than refused/)).toBeInTheDocument()
  expect(screen.queryByText(/The request was not issued/)).toBeNull()
  expect(screen.queryByRole('button', { name: /Copy request/ })).toBeNull()
})

test('a malformed or mismatched response is never shown as a receipt', async () => {
  const malformed = vi.fn(async (draft: KnowledgeRequestDraft) =>
    envelopeFor(draft, OPEN_AT, { provider: 'opendocuments.ask' }),
  )
  const harness = renderDialog({ onIssue: malformed })
  compose()
  fireEvent.click(generateButton())
  await flush()
  expect(receipt()).toBeNull()
  expect(screen.getByRole('alert')).toHaveTextContent('cannot read as a knowledge request')
  // Something answered. That the answer was unreadable says nothing about the ledger, so
  // the attempt is unconfirmed rather than refused, and there is still nothing to copy.
  expect(screen.getByText(/unconfirmed rather than refused/)).toBeInTheDocument()
  expect(screen.queryByText(/The request was not issued/)).toBeNull()
  expect(screen.queryByRole('button', { name: /Copy request/ })).toBeNull()
  harness.unmount()

  const mismatched = vi.fn(async (draft: KnowledgeRequestDraft) =>
    envelopeFor(draft, OPEN_AT, { corpus_refs: ['notion-product'] }),
  )
  renderDialog({ onIssue: mismatched })
  compose()
  fireEvent.click(generateButton())
  await flush()
  expect(receipt()).toBeNull()
  const alert = screen.getByRole('alert')
  expect(alert).toHaveTextContent('answered a different request')
  expect(alert.textContent).not.toContain('notion-product')
  expect(screen.getByText(/unconfirmed rather than refused/)).toBeInTheDocument()
  expect(screen.queryByText(/The request was not issued/)).toBeNull()
})

test('a clipboard failure is retryable on the same receipt, without issuing again', async () => {
  const copyText = vi
    .fn()
    .mockRejectedValueOnce(new Error('Clipboard access is unavailable in this browser.'))
    .mockResolvedValueOnce(undefined)
  const harness = renderDialog({ copyText })
  compose()
  fireEvent.click(generateButton())
  await screen.findByRole('region', { name: 'Issued request' })

  fireEvent.click(copyButton())
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('could not be copied'))
  expect(receipt()).not.toBeNull()
  expect(copyButton()).toBeEnabled()

  fireEvent.click(copyButton())
  await waitFor(() => expect(screen.getByText('Copied to the clipboard.')).toBeInTheDocument())
  expect(copyText).toHaveBeenCalledTimes(2)
  expect(harness.onIssue).toHaveBeenCalledTimes(1)
})

test('an expired receipt cannot be copied and is never renewed on its own', async () => {
  vi.useFakeTimers()
  const harness = renderDialog()
  compose()
  fireEvent.click(generateButton())
  await flush()
  expect(receipt()).not.toBeNull()
  expect(copyButton()).toBeEnabled()

  harness.clock.ms = OPEN_AT + WINDOW_MS
  act(() => { vi.advanceTimersByTime(WINDOW_MS) })

  expect(copyButton()).toBeDisabled()
  expect(screen.getByText(/This request has expired/)).toBeInTheDocument()
  fireEvent.click(copyButton())
  expect(harness.copyText).not.toHaveBeenCalled()
  // The window is read, never moved: the receipt still carries the issuer's own expiry.
  expect(harness.onIssue).toHaveBeenCalledTimes(1)
})

test('a receipt that arrives after the Task changed is dropped, not displayed', async () => {
  const held = heldIssue()
  const harness = renderDialog({ onIssue: held.onIssue })
  compose()
  fireEvent.click(generateButton())
  expect(held.onIssue).toHaveBeenCalledTimes(1)

  harness.rerender({ task: OTHER_TASK })
  held.releaseWith()
  await flush()

  expect(receipt()).toBeNull()
  expect(queryField()).toHaveValue(OTHER_TASK.title)
})

test('a receipt that arrives after the question changed is dropped, not displayed', async () => {
  const held = heldIssue()
  renderDialog({ onIssue: held.onIssue })
  compose()
  fireEvent.click(generateButton())
  expect(generateButton()).toBeDisabled()

  fireEvent.change(queryField(), { target: { value: `${QUERY} and the sign-off` } })
  // The edit retires the flight, so the editor is immediately usable again.
  expect(generateButton()).toBeEnabled()

  held.releaseWith()
  await flush()
  expect(receipt()).toBeNull()
})

test('a second submission while one is in flight is refused', async () => {
  const onIssue = vi.fn(() => new Promise(() => {}))
  renderDialog({ onIssue })
  compose()
  const form = document.getElementById('knowledge-request-form') as HTMLFormElement
  fireEvent.submit(form)
  fireEvent.submit(form)
  fireEvent.click(generateButton())
  await flush()
  expect(onIssue).toHaveBeenCalledTimes(1)
})

test('a receipt that arrives after the dialog unmounts updates nothing', async () => {
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  const held = heldIssue()
  const harness = renderDialog({ onIssue: held.onIssue })
  compose()
  fireEvent.click(generateButton())
  harness.unmount()

  held.releaseWith()
  await flush()

  expect(screen.queryByRole('region', { name: 'Issued request' })).toBeNull()
  expect(consoleError).not.toHaveBeenCalled()
})

test('a corpus withdrawn while it is selected stays clearable and blocks the request', async () => {
  const harness = renderDialog()
  compose()
  harness.rerender({ corpusOptions: [OPTIONS[1]] })

  const withdrawn = screen.getByRole('checkbox', { name: /nas-team-share/ })
  expect(withdrawn).toBeChecked()
  fireEvent.click(generateButton())
  await flush()
  expect(harness.onIssue).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent('not in the list this workspace is allowed to search')

  fireEvent.click(withdrawn)
  fireEvent.click(screen.getByRole('checkbox', { name: /Product notes/ }))
  fireEvent.click(generateButton())
  await flush()
  expect(harness.onIssue).toHaveBeenCalledTimes(1)
  expect(harness.onIssue.mock.calls[0][0].corpus_refs).toEqual(['notion-product'])
})

test('an astral query near the bound is counted as code points and can be issued', async () => {
  const harness = renderDialog()
  const query = '😀'.repeat(600)
  fireEvent.change(queryField(), { target: { value: query } })
  fireEvent.click(screen.getByRole('checkbox', { name: /Team share/ }))
  expect(queryField()).toHaveValue(query)
  expect(queryField()).not.toHaveAttribute('maxLength')
  expect(screen.getByText(/600 of 1000 characters/)).toBeInTheDocument()

  fireEvent.click(generateButton())
  await flush()
  expect(harness.onIssue).toHaveBeenCalledWith(expect.objectContaining({ query }))
  expect(receipt()).not.toBeNull()

  fireEvent.change(queryField(), { target: { value: '😀'.repeat(1001) } })
  expect(queryField().value).toBe('😀'.repeat(1000))
  expect(screen.getByText(/1000 of 1000 characters/)).toBeInTheDocument()
})

test('copy disables 110ms after a 100ms remaining window, without a one-second lag', async () => {
  vi.useFakeTimers()
  const expiresAt = new Date(OPEN_AT + 100).toISOString()
  const onIssue = vi.fn(async (draft: KnowledgeRequestDraft) =>
    envelopeFor(draft, OPEN_AT, { expires_at: expiresAt, requested_at: new Date(OPEN_AT).toISOString() }),
  )
  const harness = renderDialog({ onIssue })
  compose()
  fireEvent.click(generateButton())
  await flush()
  expect(copyButton()).toBeEnabled()
  expect(screen.getByText('Active')).toBeInTheDocument()

  harness.clock.ms = OPEN_AT + 110
  act(() => { vi.advanceTimersByTime(110) })

  expect(copyButton()).toBeDisabled()
  expect(screen.getByText('Expired')).toBeInTheDocument()
  expect(screen.getByText(/This request has expired/)).toBeInTheDocument()
  fireEvent.click(copyButton())
  expect(harness.copyText).not.toHaveBeenCalled()
})

test('Copy and visibility resume recheck the clock and mark expiry without waiting for the timer', async () => {
  vi.useFakeTimers()
  const onIssue = vi.fn(async (draft: KnowledgeRequestDraft) =>
    envelopeFor(draft, OPEN_AT, {
      expires_at: new Date(OPEN_AT + 100).toISOString(),
      requested_at: new Date(OPEN_AT).toISOString(),
    }),
  )
  const harness = renderDialog({ onIssue })
  compose()
  fireEvent.click(generateButton())
  await flush()
  expect(copyButton()).toBeEnabled()

  harness.clock.ms = OPEN_AT + 110
  fireEvent.click(copyButton())
  expect(harness.copyText).not.toHaveBeenCalled()
  expect(copyButton()).toBeDisabled()
  expect(screen.getByText('Expired')).toBeInTheDocument()
  harness.unmount()

  const resumed = renderDialog({ onIssue })
  compose()
  fireEvent.click(generateButton())
  await flush()
  resumed.clock.ms = OPEN_AT + 110
  act(() => { document.dispatchEvent(new Event('visibilitychange')) })
  expect(copyButton()).toBeDisabled()
  expect(screen.getByText('Expired')).toBeInTheDocument()
})

test('the expiry timer is cleared on unmount', async () => {
  vi.useFakeTimers()
  const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  const harness = renderDialog()
  compose()
  fireEvent.click(generateButton())
  await flush()
  harness.unmount()
  harness.clock.ms = OPEN_AT + WINDOW_MS
  act(() => { vi.advanceTimersByTime(WINDOW_MS) })
  expect(consoleError).not.toHaveBeenCalled()
  consoleError.mockRestore()
})

test('run connected search is offered only after a current issued receipt', async () => {
  const harness = renderDialog()
  compose()
  expect(screen.queryByRole('button', { name: 'Run connected search' })).toBeNull()
  fireEvent.click(generateButton())
  await flush()
  expect(screen.getByRole('button', { name: 'Run connected search' })).toBeEnabled()
  harness.clock.ms = OPEN_AT + WINDOW_MS
  act(() => { document.dispatchEvent(new Event('visibilitychange')) })
  expect(screen.queryByRole('button', { name: 'Run connected search' })).toBeNull()
})

/**
 * R31-A: starting values a saved Capture offered.
 *
 * A seed fills the two fields the user is about to read — the question and the purpose —
 * and nothing else. It never selects scope, never binds the Capture and never survives
 * into another Capture's session.
 */

const CAPTURE_SEED = {
  connectionHint: 'team-nas',
  contextKey: '["ws","C-0001",3]',
  launchLabel: 'Search for updated context',
  purpose: 'find_context' as const,
  query: 'Release review feedback',
}

test('a seed fills the editable question and the neutral purpose, and selects no scope', async () => {
  const harness = renderDialog({ seed: CAPTURE_SEED, task: null })

  expect(screen.getByLabelText('Question')).toHaveValue('Release review feedback')
  expect(screen.getByLabelText('Purpose')).toHaveValue('find_context')
  for (const box of screen.getAllByRole('checkbox')) expect(box).not.toBeChecked()

  // It is a prefill the user owns: they rewrite it, and what they wrote is what is sent.
  await userEvent.clear(screen.getByLabelText('Question'))
  await userEvent.type(screen.getByLabelText('Question'), QUERY)
  await userEvent.click(screen.getByRole('checkbox', { name: /Team share/ }))
  await userEvent.click(screen.getByRole('button', { name: 'Generate request' }))

  await waitFor(() => expect(harness.onIssue).toHaveBeenCalledTimes(1))
  expect(harness.onIssue.mock.calls[0][0]).toEqual({
    binding: { workspace_uid: WORKSPACE },
    corpus_refs: ['nas-team-share'],
    purpose: 'find_context',
    query: QUERY,
    result_limit: 5,
  })
})

test('a seeded question that sanitized to empty asks the user rather than inventing one', () => {
  renderDialog({ seed: { ...CAPTURE_SEED, query: '' }, task: null })
  expect(screen.getByLabelText('Question')).toHaveValue('')
})

test('another Capture is another session: typed values never carry across', async () => {
  const harness = renderDialog({ seed: CAPTURE_SEED, task: null })
  await userEvent.type(screen.getByLabelText('Question'), ' and owner')
  expect(screen.getByLabelText('Question')).toHaveValue('Release review feedback and owner')

  harness.rerender({
    seed: { ...CAPTURE_SEED, contextKey: '["ws","C-0002",1]', query: 'Release sign-off owner' },
  })
  expect(screen.getByLabelText('Question')).toHaveValue('Release sign-off owner')
})

test('with no seed the existing Task-title prefill is exactly what it was', () => {
  renderDialog()
  expect(screen.getByLabelText('Question')).toHaveValue(TASK.title)
  expect(screen.getByLabelText('Purpose')).toHaveValue('find_context')
})
