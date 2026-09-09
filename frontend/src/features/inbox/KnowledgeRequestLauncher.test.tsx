import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { KnowledgeRequestLauncher } from './KnowledgeRequestLauncher'
import { knowledgeCapture } from './knowledgeCaptureFixture'
import { knowledgeLaunchSeedFromCapture } from './knowledgeLaunchSeed'
import { jsonResponse } from '../../test/fixtures'

/**
 * The launcher is exercised through the real transport: `fetch` is the only thing
 * stubbed, so every assertion below about a route, a body or the CSRF header is an
 * assertion about what the browser would actually put on the wire. No provider is
 * contacted, because the flow has nowhere to name one.
 */

const WORKSPACE_UID = '22222222-2222-2222-2222-222222222222'
const OTHER_WORKSPACE_UID = '33333333-3333-4333-8333-333333333333'
const UPSTREAM = '66666666-6666-4666-8666-666666666666'

const teamNas = {
  alias: 'team-nas',
  corpus_refs: ['nas-team-share', 'product-notes'],
  scope: 'workspace' as const,
  upstream_workspace_uid: UPSTREAM,
}

interface ServerHandlers {
  getConnections?: () => Promise<Response>
  postConnections?: (body: Record<string, unknown>) => Promise<Response>
  postRequests?: (body: Record<string, unknown>) => Promise<Response>
}

interface Call {
  body: Record<string, unknown> | null
  headers: Record<string, string>
  method: string
  url: string
}

function stubServer(handlers: ServerHandlers) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const raw = init?.body
      calls.push({
        body: typeof raw === 'string' && raw ? JSON.parse(raw) : null,
        headers: (init?.headers ?? {}) as Record<string, string>,
        method,
        url,
      })
      if (url.includes('/api/v1/session')) {
        return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
      }
      if (url === '/api/v1/knowledge/connections' && method === 'GET') {
        return handlers.getConnections!()
      }
      if (url === '/api/v1/knowledge/connections') {
        return handlers.postConnections!(calls[calls.length - 1].body!)
      }
      if (url === '/api/v1/knowledge/requests') {
        return handlers.postRequests!(calls[calls.length - 1].body!)
      }
      throw new Error(`Unexpected request: ${url}`)
    }),
  )
  return calls
}

function policyResponse(revision: number, connections: unknown[] = [], meta?: unknown) {
  const envelope: Record<string, unknown> = { data: { connections, policy_revision: revision } }
  if (meta !== undefined) envelope.meta = meta
  return jsonResponse(envelope)
}

function refusal(code: string, status = 409) {
  return jsonResponse({ error: { code, message: 'the knowledge request was refused' } }, status)
}

/** The issuer's own answer: the document it digested, echoing the reviewed draft. */
function issuedResponse(body: Record<string, unknown>, overrides: Record<string, unknown> = {}) {
  return jsonResponse({
    data: {
      binding: body.binding,
      corpus_refs: body.corpus_refs,
      expires_at: '2099-09-08T09:37:07Z',
      purpose: body.purpose,
      query: body.query,
      request_id: '2cce8f7c-7961-5614-bc6f-ca4a093661e2',
      requested_at: '2099-09-08T09:32:07Z',
      result_limit: body.result_limit,
      schema: 'workstack.knowledge-request.v1',
    },
    meta: {
      connection_alias: 'team-nas',
      policy_revision: 1,
      replayed: false,
      state: 'pending',
      ...overrides,
    },
  })
}

function issueCalls(calls: Call[]) {
  return calls.filter((call) => call.url === '/api/v1/knowledge/requests')
}

function policyWrites(calls: Call[]) {
  return calls.filter((call) => call.url.endsWith('/connections') && call.method === 'POST')
}

async function configureConnection(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Configure connections' }))
  await user.type(screen.getByLabelText('Alias'), 'team-nas')
  await user.type(screen.getByLabelText('Upstream workspace ID'), UPSTREAM)
  await user.type(screen.getByLabelText('Corpus aliases'), 'nas-team-share, product-notes')
  await user.click(screen.getByRole('button', { name: 'Save connection policy' }))
}

async function composeAndGenerate(user: ReturnType<typeof userEvent.setup>, query: string) {
  await user.type(await screen.findByLabelText('Question'), query)
  await user.click(screen.getByRole('checkbox', { name: /nas-team-share/ }))
  await user.click(screen.getByRole('button', { name: 'Generate request' }))
}

test('first use configures a nonsecret policy, then issues and copies the server’s own request', async () => {
  const user = userEvent.setup()
  const writeText = vi.fn(async (_value: string) => {})
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  let revision = 0
  const calls = stubServer({
    getConnections: () => policyResponse(revision, revision ? [teamNas] : []),
    postConnections: () => {
      revision = 1
      return policyResponse(1, [teamNas])
    },
    postRequests: (body) => issuedResponse(body),
  })

  render(<KnowledgeRequestLauncher newIntentId={() => 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'} workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  expect(await screen.findByText('No knowledge connection yet')).toBeInTheDocument()

  await configureConnection(user)
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')

  expect(await screen.findByText('Issued request')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Copy request' }))
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))

  // What is copied is the issuer's document, field for field.
  expect(JSON.parse(writeText.mock.calls[0][0] as string)).toEqual({
    binding: { workspace_uid: WORKSPACE_UID },
    corpus_refs: ['nas-team-share'],
    expires_at: '2099-09-08T09:37:07Z',
    purpose: 'find_context',
    query: 'rollback verification owner',
    request_id: '2cce8f7c-7961-5614-bc6f-ca4a093661e2',
    requested_at: '2099-09-08T09:32:07Z',
    result_limit: 5,
    schema: 'workstack.knowledge-request.v1',
  })

  const issued = issueCalls(calls)
  expect(issued).toHaveLength(1)
  expect(issued[0].body).toEqual({
    binding: { workspace_uid: WORKSPACE_UID },
    connection_alias: 'team-nas',
    corpus_refs: ['nas-team-share'],
    intent_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    purpose: 'find_context',
    query: 'rollback verification owner',
    result_limit: 5,
  })
  expect(issued[0].headers['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect(issued[0].headers['Idempotency-Key']).toBeUndefined()

  const policyWrite = calls.find((call) => call.url.endsWith('/connections') && call.method === 'POST')!
  expect(policyWrite.body).toEqual({
    connections: [
      {
        alias: 'team-nas',
        corpus_refs: ['nas-team-share', 'product-notes'],
        upstream_workspace_uid: UPSTREAM,
      },
    ],
    expected_policy_revision: 0,
  })
  // Nothing about the question is persisted anywhere the next session could read it.
  expect(window.localStorage.length).toBe(0)
  expect(window.location.search).toBe('')
  // No Task is read, written or attached, and nothing outside the knowledge surface is called.
  expect(
    calls.every(
      (call) => call.url.startsWith('/api/v1/knowledge/') || call.url.includes('/api/v1/session'),
    ),
  ).toBe(true)
})

test('an ambiguous failure keeps the intent, and an expired one only renews on an explicit ask', async () => {
  const user = userEvent.setup()
  const minted: string[] = []
  // What the ledger actually committed, whatever the browser managed to read back.
  const committed: string[] = []
  let attempt = 0
  const calls = stubServer({
    getConnections: () => policyResponse(1, [teamNas]),
    postRequests: (body) => {
      attempt += 1
      committed.push(String(body.intent_id))
      // 1: the write commits and the answer is lost on the way back.
      // 2: the same intent, definitively expired.
      if (attempt === 1) return Promise.reject(new TypeError('network error'))
      if (attempt === 2) return refusal('request_expired')
      return issuedResponse(body)
    },
  })

  render(
    <KnowledgeRequestLauncher
      newIntentId={() => {
        const id = `0000000${minted.length}-0000-4000-8000-000000000000`
        minted.push(id)
        return id
      }}
      workspaceUid={WORKSPACE_UID}
    />,
  )
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')

  // The server may well hold this request: the browser simply never heard. Saying it was
  // not issued would be a claim nothing here established.
  expect(
    await screen.findByText(/whether this request was issued is not known/),
  ).toBeInTheDocument()
  expect(screen.getByText(/unconfirmed rather than refused/)).toBeInTheDocument()
  expect(screen.queryByText(/The request was not issued/)).not.toBeInTheDocument()
  expect(screen.queryByText('Issued request')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Copy request' })).not.toBeInTheDocument()

  // The identical body is retried explicitly. It must not become a second authorization.
  await user.click(screen.getByRole('button', { name: 'Generate request' }))
  expect(await screen.findByText(/window on this request has closed/)).toBeInTheDocument()
  const first = issueCalls(calls)
  expect(first).toHaveLength(2)
  expect(first[1].body).toEqual(first[0].body)
  expect(minted).toHaveLength(1)
  // One logical request reached the ledger twice under one intent, not two requests.
  expect(committed).toEqual([first[0].body!.intent_id, first[0].body!.intent_id])

  // Nothing renewed itself; the window reopens only because the user asked.
  await user.click(screen.getByRole('button', { name: 'Start a fresh request' }))
  await composeAndGenerate(user, 'rollback verification owner')
  expect(await screen.findByText('Issued request')).toBeInTheDocument()
  const third = issueCalls(calls)[2]
  expect(third.body!.intent_id).not.toBe(first[0].body!.intent_id)
  expect(minted).toHaveLength(2)
})

test('a lost compare-and-set re-reads the policy the server holds instead of overwriting it', async () => {
  const user = userEvent.setup()
  const serverSide = {
    alias: 'other-nas',
    corpus_refs: ['other-share'],
    scope: 'workspace' as const,
    upstream_workspace_uid: UPSTREAM,
  }
  let revision = 0
  let writes = 0
  const calls = stubServer({
    getConnections: () => policyResponse(revision, revision ? [serverSide] : []),
    postConnections: () => {
      writes += 1
      if (writes === 1) {
        // Another owner committed first. This write lands nowhere.
        revision = 7
        return refusal('policy_revision_changed')
      }
      revision = 8
      return policyResponse(8, [teamNas])
    },
    postRequests: (body) => issuedResponse(body),
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await configureConnection(user)

  // The cause comes from the definitive refusal, not from the read: the compare-and-set
  // was rejected, so "nothing was written" is established.
  expect(await screen.findByText(/policy revision it was written against is no longer/)).toBeInTheDocument()
  expect(screen.getByText(/Review the policy returned by the server/)).toBeInTheDocument()
  // The roster the server actually holds is put in front of the owner before a resave.
  const review = await screen.findByRole('region', { name: 'Policy the server holds' })
  expect(within(review).getByText('other-nas')).toBeInTheDocument()
  const policyWrites = calls.filter((call) => call.url.endsWith('/connections') && call.method === 'POST')
  expect(policyWrites).toHaveLength(1)

  // Saving again is a fresh explicit act, guarded on the revision that now exists.
  await user.click(screen.getByRole('button', { name: 'Save connection policy' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Search team-nas' })).toBeInTheDocument())
  const resave = calls.filter((call) => call.url.endsWith('/connections') && call.method === 'POST')[1]
  expect(resave.body).toMatchObject({ expected_policy_revision: 7 })
})

test('a lost compare-and-set whose re-read fails says the policy is unknown, never empty', async () => {
  const user = userEvent.setup()
  const serverSide = {
    alias: 'other-nas',
    corpus_refs: ['other-share'],
    scope: 'workspace' as const,
    upstream_workspace_uid: UPSTREAM,
  }
  // Reads stop answering exactly when the lost compare-and-set requires one. A read is
  // allowed its transport-level retry, so this is a state rather than a call count.
  let readsAnswer = true
  let revision = 0
  let held: unknown[] = []
  const calls = stubServer({
    getConnections: () =>
      readsAnswer
        ? policyResponse(revision, held)
        : Promise.reject(new TypeError('network error')),
    postConnections: () => {
      // Another owner committed first, and the network drops before the re-read.
      revision = 7
      held = [serverSide]
      readsAnswer = false
      return refusal('policy_revision_changed')
    },
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await configureConnection(user)

  // Nothing read the server, so nothing here may say what the server holds.
  expect(await screen.findByText('What the server holds now is unknown')).toBeInTheDocument()
  expect(screen.queryByText('No connection at all.')).not.toBeInTheDocument()
  // Nothing was read, so the review that would invite a re-save is not offered either.
  expect(screen.queryByText(/Review the policy returned by the server/)).not.toBeInTheDocument()
  expect(screen.getByText(/policy revision the server holds is not known/)).toBeInTheDocument()
  // Writing needs a revision a read returned, so saving waits for one.
  expect(screen.getByRole('button', { name: 'Save connection policy' })).toBeDisabled()
  const settings = screen.getByRole('region', { name: 'Knowledge connection settings' })
  expect(policyWrites(calls)).toHaveLength(1)

  // Only the read the owner explicitly asks for establishes the roster.
  readsAnswer = true
  await user.click(within(settings).getByRole('button', { name: 'Read the policy again' }))
  expect(await screen.findByText('other-nas')).toBeInTheDocument()
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Save connection policy' })).toBeEnabled(),
  )
  expect(screen.queryByText('What the server holds now is unknown')).not.toBeInTheDocument()
  expect(policyWrites(calls)).toHaveLength(1)
})

test('an unconfirmed save whose re-read fails stays unknown until a read says otherwise', async () => {
  const user = userEvent.setup()
  let readsAnswer = true
  const calls = stubServer({
    getConnections: () =>
      readsAnswer ? policyResponse(0, []) : Promise.reject(new TypeError('network error')),
    // The write may well have landed; the browser never heard, and the re-read that
    // would settle it does not answer either.
    postConnections: () => {
      readsAnswer = false
      return Promise.reject(new TypeError('network error'))
    },
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await configureConnection(user)

  expect(await screen.findByText('What the server holds now is unknown')).toBeInTheDocument()
  expect(screen.queryByText('No connection at all.')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save connection policy' })).toBeDisabled()
  // The body is never sent again on its own.
  expect(policyWrites(calls)).toHaveLength(1)

  const settings = screen.getByRole('region', { name: 'Knowledge connection settings' })
  readsAnswer = true
  await user.click(within(settings).getByRole('button', { name: 'Read the policy again' }))

  // A successful read is the only thing that can establish that the server holds nothing.
  expect(await screen.findByText('No knowledge connection yet')).toBeInTheDocument()
  expect(screen.queryByText('What the server holds now is unknown')).not.toBeInTheDocument()
  expect(policyWrites(calls)).toHaveLength(1)
})

test('a committed save whose response was lost is not reported as another owner’s write', async () => {
  const user = userEvent.setup()
  let revision = 0
  let held: unknown[] = []
  const calls = stubServer({
    getConnections: () => policyResponse(revision, held),
    // The server commits the owner's own replacement and then the response is lost. The
    // re-read that follows answers, and shows exactly what this save wrote.
    postConnections: () => {
      revision = 1
      held = [teamNas]
      return Promise.reject(new TypeError('network error'))
    },
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await configureConnection(user)

  // The outcome of the write stays unknown, and the read reports only the roster.
  expect(await screen.findByText(/did not confirm the save/)).toBeInTheDocument()
  const review = await screen.findByRole('region', { name: 'Policy the server holds' })
  expect(within(review).getByText('team-nas')).toBeInTheDocument()
  expect(screen.getByText(/Review the policy returned by the server/)).toBeInTheDocument()
  // A successful read establishes the roster, never who wrote it or whether this save landed.
  expect(screen.queryByText(/Another owner/)).not.toBeInTheDocument()
  expect(screen.queryByText(/[Nn]othing (was overwritten|here was written)/)).not.toBeInTheDocument()
  // The body is never sent again to settle the outcome.
  expect(policyWrites(calls)).toHaveLength(1)
})

test('a corpus refusal does not announce a re-read that then fails', async () => {
  const user = userEvent.setup()
  let readsAnswer = true
  stubServer({
    getConnections: () =>
      readsAnswer ? policyResponse(1, [teamNas]) : Promise.reject(new TypeError('network error')),
    // The refusal is definitive and triggers a re-read; that read never answers, in
    // either of the transport's two real attempts.
    postRequests: () => {
      readsAnswer = false
      return refusal('corpus_not_granted')
    },
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')

  expect(await screen.findByText(/requested corpus is not granted/)).toBeInTheDocument()
  // The re-read the refusal asked for did not answer, so nothing may claim it happened.
  expect(screen.queryByText(/has been re-read/)).not.toBeInTheDocument()
  expect(await screen.findByText(/last policy that was read successfully/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Search team-nas' })).toBeDisabled()
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()

  // The explicit read is still the only thing that clears the stale marking.
  readsAnswer = true
  await user.click(screen.getByRole('button', { name: 'Read the policy again' }))
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Search team-nas' })).toBeEnabled(),
  )
  expect(screen.queryByText(/last policy that was read successfully/)).not.toBeInTheDocument()
})

test('a first read that fails is not an empty policy at revision zero', async () => {
  const user = userEvent.setup()
  const calls = stubServer({
    getConnections: () => Promise.reject(new TypeError('network error')),
    postConnections: () => policyResponse(1, [teamNas]),
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))

  expect(await screen.findByText('The connection policy is unavailable')).toBeInTheDocument()
  expect(screen.queryByText('No knowledge connection yet')).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Connection settings' }))
  expect(screen.getByText(/policy revision the server holds is not known/)).toBeInTheDocument()
  expect(screen.queryByText(/^Policy revision /)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save connection policy' })).toBeDisabled()
  expect(policyWrites(calls)).toHaveLength(0)
})

test('a re-read that fails leaves the last policy stale, and holds requests until it succeeds', async () => {
  const user = userEvent.setup()
  let readsAnswer = true
  stubServer({
    getConnections: () =>
      readsAnswer ? policyResponse(1, [teamNas]) : Promise.reject(new TypeError('network error')),
    // The refusal requires a re-read, and that read does not answer.
    postRequests: () => {
      readsAnswer = false
      return refusal('policy_revision_changed')
    },
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')

  expect(await screen.findByText(/last policy that was read successfully/)).toBeInTheDocument()
  // The stale roster is still readable, and nothing can be asked against it.
  expect(screen.getByRole('button', { name: 'Search team-nas' })).toBeDisabled()
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()

  readsAnswer = true
  await user.click(screen.getByRole('button', { name: 'Read the policy again' }))
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Search team-nas' })).toBeEnabled(),
  )
  expect(screen.queryByText(/last policy that was read successfully/)).not.toBeInTheDocument()
})

test('an answer for another connection or policy revision never becomes a receipt', async () => {
  const user = userEvent.setup()
  stubServer({
    getConnections: () => policyResponse(1, [teamNas]),
    // Well formed, and about something else entirely.
    postRequests: (body) => issuedResponse(body, { connection_alias: 'other-nas', policy_revision: 9 }),
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')

  // The mismatched answer is dropped, and what happened to the reviewed request is
  // reported as unknown rather than as a refusal nothing here established.
  expect(await screen.findByText(/was about a different connection/)).toBeInTheDocument()
  expect(screen.getByText(/unconfirmed rather than refused/)).toBeInTheDocument()
  expect(screen.queryByText(/The request was not issued/)).not.toBeInTheDocument()
  expect(screen.queryByText('Issued request')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Copy request' })).not.toBeInTheDocument()
})

test('a successful answer this screen cannot read leaves the outcome unknown', async () => {
  const user = userEvent.setup()
  // A 2xx whose body is not the issued document at all: a proxy page, then a document
  // that breaks the closed schema. Either way the ledger may already hold the request.
  const unreadable = () => Promise.resolve(new Response('<html>gateway</html>', { status: 200 }))
  const calls = stubServer({
    getConnections: () => policyResponse(1, [teamNas]),
    postRequests: unreadable,
  })

  const view = render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')

  expect(
    await screen.findByText(/whether this request was issued is not known/),
  ).toBeInTheDocument()
  expect(screen.queryByText(/The request was not issued/)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Copy request' })).not.toBeInTheDocument()
  expect(issueCalls(calls)).toHaveLength(1)
  view.unmount()

  stubServer({
    getConnections: () => policyResponse(1, [teamNas]),
    postRequests: () => jsonResponse({
      data: { request_id: 'not-a-uuid', schema: 'workstack.knowledge-request.v1' },
      meta: { connection_alias: 'team-nas', policy_revision: 1, replayed: false, state: 'pending' },
    }),
  })
  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')

  expect(
    await screen.findByText(/whether this request was issued is not known/),
  ).toBeInTheDocument()
  expect(screen.queryByText(/The request was not issued/)).not.toBeInTheDocument()
  expect(screen.queryByText('Issued request')).not.toBeInTheDocument()
})

test('a policy read that settles after the workspace changed does not populate the screen', async () => {
  const user = userEvent.setup()
  let release = () => {}
  const pending = new Promise<void>((resolve) => { release = resolve })
  stubServer({
    getConnections: async () => {
      await pending
      return policyResponse(1, [teamNas])
    },
  })

  const view = render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  expect(await screen.findByText('Reading the connection policy…')).toBeInTheDocument()

  view.rerender(<KnowledgeRequestLauncher workspaceUid={OTHER_WORKSPACE_UID} />)
  release()
  await waitFor(() =>
    expect(screen.queryByText('Reading the connection policy…')).not.toBeInTheDocument(),
  )
  expect(screen.queryByRole('button', { name: 'Search team-nas' })).not.toBeInTheDocument()
})

test('a backend with no knowledge ledger says so instead of offering a request', async () => {
  const user = userEvent.setup()
  stubServer({ getConnections: () => refusal('knowledge_backend_unsupported') })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))

  expect(await screen.findByText(/keeps no knowledge ledger/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /^Search team-nas$/ })).not.toBeInTheDocument()
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()
})

test('the settings form collects no endpoint, path, account or token', async () => {
  const user = userEvent.setup()
  stubServer({ getConnections: () => policyResponse(0, []) })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Configure connections' }))

  const fields = screen.getAllByRole('textbox').map((field) => field.getAttribute('placeholder'))
  expect(fields).toEqual(['team-nas', UPSTREAM, 'nas-team-share, product-notes'])
  for (const forbidden of ['Endpoint', 'Token', 'Password', 'Folder', 'Path', 'Account', 'URL']) {
    expect(screen.queryByLabelText(forbidden)).not.toBeInTheDocument()
  }
  expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0)
})

/**
 * The two capacity refusals. Both are decisions the issuer made and then discarded its
 * planned record for, so both may be reported as "not issued" — and neither one may offer
 * a fresh request, because a new intent is another record a full ledger cannot hold.
 */
test('a full ledger settles with its own capacity copy and offers no fresh request', async () => {
  const user = userEvent.setup()
  const minted: string[] = []
  const calls = stubServer({
    getConnections: () => policyResponse(1, [teamNas]),
    // The ledger already holds its limit of records. Nothing the browser asks changes that.
    postRequests: () => refusal('ledger_full'),
  })

  render(
    <KnowledgeRequestLauncher
      newIntentId={() => {
        const id = `0000000${minted.length}-0000-4000-8000-000000000000`
        minted.push(id)
        return id
      }}
      workspaceUid={WORKSPACE_UID}
    />,
  )
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')

  // The authored line the ledger's own limit deserves, in front of the user rather than
  // dead in a table nothing reads.
  expect(
    await screen.findByText(
      'The knowledge request limit has been reached. Cleanup is not available in this version.',
    ),
  ).toBeInTheDocument()
  // Not left pressing Generate against a decision that will not change.
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()
  expect(screen.queryByText(/The request was not issued/)).not.toBeInTheDocument()
  // Determinate: the issuer decided, so nothing here may call the outcome unknown.
  expect(screen.queryByText(/whether this request was issued is not known/)).not.toBeInTheDocument()
  // A new intent is another record. Offering one would be a loop that cannot end.
  expect(screen.queryByRole('button', { name: 'Start a fresh request' })).not.toBeInTheDocument()
  // The server's own message is never echoed; every line here was authored here.
  expect(screen.queryByText(/the knowledge request was refused/)).not.toBeInTheDocument()

  // The refusal neither re-sent the body nor rotated the intent behind the user's back.
  expect(issueCalls(calls)).toHaveLength(1)
  expect(minted).toEqual(['00000000-0000-4000-8000-000000000000'])
  // `refetch: false` — a full ledger is not a stale policy, so nothing was re-read.
  expect(calls.filter((call) => call.url.endsWith('/connections') && call.method === 'GET'))
    .toHaveLength(1)
  expect(policyWrites(calls)).toHaveLength(0)

  // A different question later is a different record, and it is refused the same way.
  await user.click(screen.getByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'a different question entirely')
  expect(
    await screen.findByText(
      'The knowledge request limit has been reached. Cleanup is not available in this version.',
    ),
  ).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Start a fresh request' })).not.toBeInTheDocument()
  const issued = issueCalls(calls)
  expect(issued).toHaveLength(2)
  expect(issued[1].body!.query).toBe('a different question entirely')
})

test('a record too large for the ledger is a determinate refusal, not a commit-unknown', async () => {
  const user = userEvent.setup()
  const calls = stubServer({
    getConnections: () => policyResponse(1, [teamNas]),
    // The byte limit is reached before the record count is: the issuer validated the
    // document it planned to append, refused it, and never saved.
    postRequests: () => refusal('document_too_large'),
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')

  expect(
    await screen.findByText(
      'Knowledge request storage is full. Cleanup is not available in this version.',
    ),
  ).toBeInTheDocument()
  // The old classification implied the write might have landed. It cannot have.
  expect(screen.queryByText(/whether this request was issued is not known/)).not.toBeInTheDocument()
  expect(screen.queryByText(/unconfirmed rather than refused/)).not.toBeInTheDocument()
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Start a fresh request' })).not.toBeInTheDocument()
  // Nothing was sent again, and the server's own message is not echoed.
  expect(issueCalls(calls)).toHaveLength(1)
  expect(screen.queryByText(/the knowledge request was refused/)).not.toBeInTheDocument()
})

test('an inherited error name settles nothing and shows no inherited member as copy', async () => {
  // `code` is a string the server puts on the wire, so these can arrive. None of them is a
  // refusal this build published, and an `Object.prototype` member is not copy.
  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf']) {
    const user = userEvent.setup()
    const calls = stubServer({
      getConnections: () => policyResponse(1, [teamNas]),
      postRequests: () => refusal(code),
    })

    const view = render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
    await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
    await user.click(await screen.findByRole('button', { name: 'Search team-nas' }))
    await composeAndGenerate(user, 'rollback verification owner')

    // An unpublished code keeps the pre-existing generic outcome: unknown, in the editor.
    expect(
      await screen.findByText(/whether this request was issued is not known/),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('Question')).toBeInTheDocument()
    // It never settles the attempt, so no capacity or refusal notice is claimed for it.
    expect(screen.queryByText(/Cleanup is not available in this version/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start a fresh request' })).not.toBeInTheDocument()
    // No function, prototype or server string reached the screen.
    expect(document.body.textContent).not.toMatch(/function |\[object Object\]/)
    expect(screen.queryByText(/the knowledge request was refused/)).not.toBeInTheDocument()
    expect(issueCalls(calls)).toHaveLength(1)
    view.unmount()
  }
})

/**
 * R31-A: the same launcher, opened from a saved Capture with starting values.
 *
 * Everything below is about what a seed may and may not do. It renames the control, fills
 * the editable question and suggests a connection alias; it never binds the Capture to a
 * request, never guarantees the old document is the one searched and never claims the
 * Capture was refreshed. The wire assertions are the proof: the body is the same
 * workspace-only shape the unseeded flow sends.
 */

function seededCapture(connectionRef: string, title = 'Rollback verification owner') {
  const base = knowledgeCapture()
  return knowledgeCapture({
    source: { ...base.source, connection_ref: connectionRef, display_title: title },
  })
}

function seedFor(connectionRef: string, title?: string) {
  return knowledgeLaunchSeedFromCapture(seededCapture(connectionRef, title), WORKSPACE_UID)
}

function policyReads(calls: Call[]) {
  return calls.filter((call) => call.url === '/api/v1/knowledge/connections' && call.method === 'GET')
}

test('a seeded entry reads nothing until it is opened, then opens the editor on a granted hint', async () => {
  const user = userEvent.setup()
  const calls = stubServer({
    getConnections: () => policyResponse(1, [teamNas]),
    postRequests: (body) => issuedResponse(body),
  })

  render(
    <KnowledgeRequestLauncher
      newIntentId={() => 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}
      seed={seedFor('team-nas')}
      workspaceUid={WORKSPACE_UID}
    />,
  )
  // A mounted drawer entry is a button and nothing else: no policy read, no search and no
  // provider call. The Capture it came from is not read again either.
  expect(calls).toHaveLength(0)
  expect(screen.queryByRole('button', { name: 'Search knowledge' })).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  // The alias the Capture names is only used once this read has returned it.
  const question = await screen.findByLabelText('Question')
  expect(policyReads(calls)).toHaveLength(1)
  expect(question).toHaveValue('Rollback verification owner')
  expect(screen.getByLabelText('Purpose')).toHaveValue('find_context')
  // Scope is never seeded: no corpus is inferred from the Capture.
  for (const box of screen.getAllByRole('checkbox')) expect(box).not.toBeChecked()

  // The user rewrites the question they were given and picks the scope themselves.
  await user.clear(question)
  await user.type(question, 'rollback verification owner today')
  await user.click(screen.getByRole('checkbox', { name: /nas-team-share/ }))
  await user.click(screen.getByRole('button', { name: 'Generate request' }))

  expect(await screen.findByText('Issued request')).toBeInTheDocument()
  const issued = issueCalls(calls)
  expect(issued).toHaveLength(1)
  // The reviewed title may live in the query. Nothing else about the Capture may travel:
  // the binding is workspace-only and the closed body has no room for a capture field.
  expect(issued[0].body).toEqual({
    binding: { workspace_uid: WORKSPACE_UID },
    connection_alias: 'team-nas',
    corpus_refs: ['nas-team-share'],
    intent_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    purpose: 'find_context',
    query: 'rollback verification owner today',
    result_limit: 5,
  })
  expect(JSON.stringify(issued[0].body)).not.toContain('C-0001')
  // Nothing on screen says the capture was refreshed or is now current. The one place
  // the word appears at all is the purpose the user may pick for themselves.
  expect(document.body.textContent).not.toMatch(/refreshed|up to date|now current/i)
})

test('a revoked or unknown hint selects nothing and leaves the roster to choose from', async () => {
  const user = userEvent.setup()
  const calls = stubServer({ getConnections: () => policyResponse(4, [teamNas]) })

  render(<KnowledgeRequestLauncher seed={seedFor('retired-nas')} workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))

  // The roster the server returned is what is offered: the Capture's own alias is not
  // added to it, and no editor is opened against a connection nobody granted.
  expect(await screen.findByRole('button', { name: 'Search team-nas' })).toBeInTheDocument()
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()
  expect(screen.queryByText('retired-nas')).not.toBeInTheDocument()
  expect(issueCalls(calls)).toHaveLength(0)

  // The owner picks a currently granted connection instead, and the seed still fills the
  // question they review.
  await user.click(screen.getByRole('button', { name: 'Search team-nas' }))
  expect(await screen.findByLabelText('Question')).toHaveValue('Rollback verification owner')
})

test('a seeded flow re-reads the policy every time it is opened; the Inbox entry does not', async () => {
  const user = userEvent.setup()
  const calls = stubServer({ getConnections: () => policyResponse(1, [teamNas]) })

  const seeded = render(
    <KnowledgeRequestLauncher seed={seedFor('retired-nas')} workspaceUid={WORKSPACE_UID} />,
  )
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  await screen.findByRole('button', { name: 'Search team-nas' })
  await user.click(screen.getByRole('button', { name: 'Close' }))
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  // A roster this screen happens to be holding is not evidence of what is granted now.
  await waitFor(() => expect(policyReads(calls)).toHaveLength(2))
  seeded.unmount()

  const plain = render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await screen.findByRole('button', { name: 'Search team-nas' })
  await user.click(screen.getByRole('button', { name: 'Close' }))
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await screen.findByRole('button', { name: 'Search team-nas' })
  // The unchanged Inbox behaviour: one read, reused.
  expect(policyReads(calls)).toHaveLength(3)
  plain.unmount()
})

test('a missing policy offers the existing authored state rather than an invented scope', async () => {
  const user = userEvent.setup()
  const calls = stubServer({ getConnections: () => policyResponse(0, []) })

  render(<KnowledgeRequestLauncher seed={seedFor('team-nas')} workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))

  expect(await screen.findByText('No knowledge connection yet')).toBeInTheDocument()
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()
  expect(screen.queryByText('team-nas')).not.toBeInTheDocument()
  expect(issueCalls(calls)).toHaveLength(0)
})

test('rapid entry clicks open one session, and a second generate never doubles a request', async () => {
  const user = userEvent.setup()
  let release!: () => void
  const calls = stubServer({
    getConnections: () => policyResponse(1, [teamNas]),
    postRequests: (body) =>
      new Promise<Response>((resolve) => {
        release = () => resolve(issuedResponse(body))
      }),
  })

  render(
    <KnowledgeRequestLauncher
      newIntentId={() => 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}
      seed={seedFor('team-nas')}
      workspaceUid={WORKSPACE_UID}
    />,
  )
  const entry = screen.getByRole('button', { name: 'Search for updated context' })
  await user.click(entry)
  await user.click(entry)

  // Two clicks, one flow: one editor, seeded once.
  expect(await screen.findByLabelText('Question')).toHaveValue('Rollback verification owner')
  expect(screen.getAllByLabelText('Question')).toHaveLength(1)

  await user.click(screen.getByRole('checkbox', { name: /nas-team-share/ }))
  const generate = screen.getByRole('button', { name: 'Generate request' })
  await user.click(generate)
  // The attempt is still in flight. Pressing again is not a second request.
  await user.click(generate)
  expect(issueCalls(calls)).toHaveLength(1)

  await act(async () => { release() })
  expect(await screen.findByText('Issued request')).toBeInTheDocument()
  expect(issueCalls(calls)).toHaveLength(1)
})

test('switching Capture context, and back again, drops what the previous one started', async () => {
  const user = userEvent.setup()
  stubServer({ getConnections: () => policyResponse(1, [teamNas]) })

  const first = seedFor('team-nas', 'Rollback verification owner')
  const second = knowledgeLaunchSeedFromCapture(
    knowledgeCapture({
      id: 'C-0002',
      source: {
        ...knowledgeCapture().source,
        connection_ref: 'team-nas',
        display_title: 'Release sign-off owner',
      },
    }),
    WORKSPACE_UID,
  )
  const view = render(<KnowledgeRequestLauncher seed={first} workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  expect(await screen.findByLabelText('Question')).toHaveValue('Rollback verification owner')

  // B: a different Capture is a different question. Nothing the first one opened survives.
  view.rerender(<KnowledgeRequestLauncher seed={second} workspaceUid={WORKSPACE_UID} />)
  await waitFor(() => expect(screen.queryByLabelText('Question')).not.toBeInTheDocument())
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  expect(await screen.findByLabelText('Question')).toHaveValue('Release sign-off owner')

  // Back to A: the editor restarts on its own values rather than keeping B's.
  view.rerender(<KnowledgeRequestLauncher seed={first} workspaceUid={WORKSPACE_UID} />)
  await waitFor(() => expect(screen.queryByLabelText('Question')).not.toBeInTheDocument())
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  expect(await screen.findByLabelText('Question')).toHaveValue('Rollback verification owner')

  // A different workspace closes it entirely rather than carrying the question across.
  view.rerender(<KnowledgeRequestLauncher seed={first} workspaceUid={OTHER_WORKSPACE_UID} />)
  await waitFor(() => expect(screen.queryByLabelText('Question')).not.toBeInTheDocument())
})

/**
 * Closing a seeded flow is the owner saying they are done with it. Both cases below are
 * about a *valid* server answer that arrives afterwards: it is not wrong, it is simply no
 * longer this screen's, and neither one may put a surface back in front of the user.
 */
test('a seeded policy read that answers after Close does not reopen the editor', async () => {
  const user = userEvent.setup()
  let release = () => {}
  const pending = new Promise<void>((resolve) => { release = resolve })
  const calls = stubServer({
    getConnections: async () => {
      await pending
      return policyResponse(1, [teamNas])
    },
  })

  render(<KnowledgeRequestLauncher seed={seedFor('team-nas')} workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  expect(await screen.findByText('Reading the connection policy…')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Close' }))
  expect(screen.queryByText('Reading the connection policy…')).not.toBeInTheDocument()

  // The read the owner started answers, and the alias the Capture named is in the roster.
  // The hint was spent by the close, so nothing is selected and nothing is reopened.
  await act(async () => { release() })
  await waitFor(() => expect(policyReads(calls)).toHaveLength(1))
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Search team-nas' })).not.toBeInTheDocument()
  expect(issueCalls(calls)).toHaveLength(0)
  // Only the entry the drawer offers is left on screen.
  expect(screen.getByRole('button', { name: 'Search for updated context' })).toBeInTheDocument()
})

test('a settled issue refusal that arrives after Cancel then Close reopens nothing', async () => {
  const user = userEvent.setup()
  let release!: () => void
  const calls = stubServer({
    getConnections: () => policyResponse(1, [teamNas]),
    postRequests: () =>
      new Promise<Response>((resolve) => {
        release = () => resolve(refusal('request_expired'))
      }),
  })

  render(<KnowledgeRequestLauncher seed={seedFor('team-nas')} workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  await screen.findByLabelText('Question')
  await user.click(screen.getByRole('checkbox', { name: /nas-team-share/ }))
  await user.click(screen.getByRole('button', { name: 'Generate request' }))
  expect(issueCalls(calls)).toHaveLength(1)

  // Cancel leaves the editor for the roster it was chosen from; Close then ends the flow
  // while the attempt the owner started is still outstanding.
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(await screen.findByRole('button', { name: 'Search team-nas' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Close' }))
  expect(screen.queryByRole('button', { name: 'Search team-nas' })).not.toBeInTheDocument()

  await act(async () => { release() })
  // A closed refusal normally reopens the chooser with its notice. Here the flow it would
  // have settled is gone, so no surface returns and no line is written to one.
  await waitFor(() => expect(issueCalls(calls)).toHaveLength(1))
  expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Search team-nas' })).not.toBeInTheDocument()
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()
  expect(screen.queryByText(/The window on this request has closed/)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Start a fresh request' })).not.toBeInTheDocument()
  // `request_expired` does not re-read the policy, and the close must not start one either.
  expect(policyReads(calls)).toHaveLength(1)
  expect(screen.getByRole('button', { name: 'Search for updated context' })).toBeInTheDocument()
})

/**
 * The storage region. Everything below is about the same promise: these numbers are the
 * ones an explicit read or save returned, they are labelled as that moment and no other,
 * and whatever the server says or fails to say about them, the policy flow beside them is
 * untouched — the roster still lists, the request still issues, the save still saves.
 */

const occupancyMeta = {
  occupancy: { byte_bound: 262144, encoded_bytes: 18432, request_bound: 200, request_count: 7 },
}

function usageRegion() {
  return screen.getByRole('region', { name: 'Search request storage' })
}

test('the storage region reports the store as the policy read found it, at no extra cost', async () => {
  const user = userEvent.setup()
  const calls = stubServer({ getConnections: () => policyResponse(1, [teamNas], occupancyMeta) })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await screen.findByRole('button', { name: 'Search team-nas' })

  const region = usageRegion()
  const [requests, size] = within(region).getAllByRole('progressbar')
  expect(requests).toHaveAttribute('aria-valuetext', '7 of 200 requests')
  expect(requests).toHaveAttribute('aria-valuemax', '200')
  expect(size).toHaveAttribute('aria-valuetext', '18,432 of 262,144 bytes')
  expect(within(region).getByText('At last policy read/save')).toBeInTheDocument()

  // The observation rides the policy read. No occupancy route, no second read, no timer.
  expect(policyReads(calls)).toHaveLength(1)
  expect(calls.every((call) => !call.url.includes('occupancy'))).toBe(true)
  await new Promise((resolve) => setTimeout(resolve, 30))
  expect(policyReads(calls)).toHaveLength(1)
})

test('a server that publishes no usage leaves the region unavailable and the flow intact', async () => {
  const user = userEvent.setup()
  const calls = stubServer({
    // The old server: policy `data` and nothing beside it.
    getConnections: () => policyResponse(1, [teamNas]),
    postRequests: (body) => issuedResponse(body),
  })

  render(
    <KnowledgeRequestLauncher
      newIntentId={() => 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'}
      workspaceUid={WORKSPACE_UID}
    />,
  )
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await screen.findByRole('button', { name: 'Search team-nas' })

  expect(within(usageRegion()).getByText('Storage usage unavailable')).toBeInTheDocument()
  // Unavailable usage is not an empty store and not a refusal: the search still runs.
  await user.click(screen.getByRole('button', { name: 'Search team-nas' }))
  await composeAndGenerate(user, 'rollback verification owner')
  expect(await screen.findByText('Issued request')).toBeInTheDocument()
  expect(issueCalls(calls)).toHaveLength(1)
})

test('a malformed usage block is unavailable, and never a failed read', async () => {
  const user = userEvent.setup()
  stubServer({
    // A count above its own bound: not an observation this screen may show.
    getConnections: () =>
      policyResponse(1, [teamNas], {
        occupancy: {
          byte_bound: 262144,
          encoded_bytes: 18432,
          request_bound: 200,
          request_count: 900,
        },
      }),
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))

  expect(await screen.findByRole('button', { name: 'Search team-nas' })).toBeInTheDocument()
  expect(within(usageRegion()).getByText('Storage usage unavailable')).toBeInTheDocument()
  expect(screen.queryByText(/900 of 200/)).not.toBeInTheDocument()
  // The read itself succeeded, so nothing is marked stale.
  expect(screen.queryByRole('button', { name: 'Read the policy again' })).not.toBeInTheDocument()
})

test('a read that did not answer keeps the numbers but marks them out of date', async () => {
  const user = userEvent.setup()
  let read = 0
  stubServer({
    getConnections: () => {
      read += 1
      return read === 1
        ? policyResponse(1, [teamNas], occupancyMeta)
        : Promise.reject(new TypeError('network error'))
    },
  })

  render(<KnowledgeRequestLauncher seed={seedFor('retired-nas')} workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  await screen.findByRole('button', { name: 'Search team-nas' })
  expect(within(usageRegion()).getByText('At last policy read/save')).toBeInTheDocument()

  // A seeded flow re-reads on every open. This one does not answer.
  await user.click(screen.getByRole('button', { name: 'Close' }))
  await user.click(screen.getByRole('button', { name: 'Search for updated context' }))
  await screen.findByText(/whether the server still holds it is not known/)

  const region = usageRegion()
  expect(within(region).getByText(/Out of date/)).toBeInTheDocument()
  expect(within(region).queryByText('At last policy read/save')).not.toBeInTheDocument()
  // Retained, not invented and not zeroed: it is still what the last read actually saw.
  expect(within(region).getByText('7 of 200 requests')).toBeInTheDocument()
})

test('an explicit save updates the usage and sends the same closed body', async () => {
  const user = userEvent.setup()
  let revision = 0
  const calls = stubServer({
    getConnections: () =>
      policyResponse(revision, revision ? [teamNas] : [], revision ? occupancyMeta : undefined),
    postConnections: () => {
      revision = 1
      return policyResponse(1, [teamNas], {
        occupancy: {
          byte_bound: 262144,
          encoded_bytes: 20480,
          request_bound: 200,
          request_count: 8,
        },
      })
    },
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  expect(await screen.findByText('No knowledge connection yet')).toBeInTheDocument()
  expect(within(usageRegion()).getByText('Storage usage unavailable')).toBeInTheDocument()

  await configureConnection(user)
  await screen.findByRole('button', { name: 'Search team-nas' })

  // The save's own answer is the observation, so the region moves without a second read.
  const region = usageRegion()
  expect(within(region).getByText('8 of 200 requests')).toBeInTheDocument()
  expect(within(region).getByText('20,480 of 262,144 bytes')).toBeInTheDocument()
  expect(within(region).getByText('At last policy read/save')).toBeInTheDocument()
  expect(policyReads(calls)).toHaveLength(1)

  const writes = policyWrites(calls)
  expect(writes).toHaveLength(1)
  expect(Object.keys(writes[0].body!).sort()).toEqual(['connections', 'expected_policy_revision'])
  expect(JSON.stringify(writes[0].body)).not.toContain('occupancy')
})

test('a malformed usage on the save answer leaves the save accepted', async () => {
  const user = userEvent.setup()
  let revision = 0
  const calls = stubServer({
    getConnections: () => policyResponse(revision, revision ? [teamNas] : []),
    postConnections: () => {
      revision = 1
      // A bound of zero: unreadable as usage, and irrelevant to whether the roster saved.
      return policyResponse(1, [teamNas], {
        occupancy: { byte_bound: 0, encoded_bytes: 0, request_bound: 0, request_count: 0 },
      })
    },
  })

  render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await screen.findByText('No knowledge connection yet')
  await configureConnection(user)

  // The roster is on screen, so the save landed. Nothing was re-sent, nothing is unknown.
  expect(await screen.findByRole('button', { name: 'Search team-nas' })).toBeInTheDocument()
  expect(screen.queryByText(/the outcome is unknown/)).not.toBeInTheDocument()
  expect(screen.queryByText(/was not saved/)).not.toBeInTheDocument()
  expect(policyWrites(calls)).toHaveLength(1)
  expect(within(usageRegion()).getByText('Storage usage unavailable')).toBeInTheDocument()
})

test('a workspace change drops the observation with everything else it read', async () => {
  const user = userEvent.setup()
  const seen: string[] = []
  stubServer({
    getConnections: () => {
      seen.push('read')
      // Only the first workspace's server publishes usage.
      return seen.length === 1
        ? policyResponse(1, [teamNas], occupancyMeta)
        : policyResponse(1, [teamNas])
    },
  })

  const view = render(<KnowledgeRequestLauncher workspaceUid={WORKSPACE_UID} />)
  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await screen.findByRole('button', { name: 'Search team-nas' })
  expect(within(usageRegion()).getByText('7 of 200 requests')).toBeInTheDocument()

  act(() => {
    view.rerender(<KnowledgeRequestLauncher workspaceUid={OTHER_WORKSPACE_UID} />)
  })
  // The reset closes the chooser outright: nothing from the previous workspace survives.
  expect(screen.queryByRole('region', { name: 'Search request storage' })).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Search knowledge' }))
  await screen.findByRole('button', { name: 'Search team-nas' })
  const region = usageRegion()
  expect(within(region).getByText('Storage usage unavailable')).toBeInTheDocument()
  expect(within(region).queryByText('7 of 200 requests')).not.toBeInTheDocument()
})
