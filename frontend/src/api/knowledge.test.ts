import { expect, test, vi } from 'vitest'
import { ApiError } from './transport'
import {
  fetchKnowledgeConnections,
  issueKnowledgeRequest,
  replaceKnowledgeConnections,
  type KnowledgeIssueBody,
} from './knowledge'
import { jsonResponse } from '../test/fixtures'

const WORKSPACE_UID = '210aefb5-edf0-4841-af90-ffb5f778a255'
const INTENT_ID = '11111111-1111-4111-8111-111111111111'
const REQUEST_ID = '2cce8f7c-7961-5614-bc6f-ca4a093661e2'

const policyEnvelope = {
  data: {
    connections: [
      {
        alias: 'team-nas',
        corpus_refs: ['nas-team-share', 'product-notes'],
        scope: 'workspace',
        upstream_workspace_uid: '66666666-6666-4666-8666-666666666666',
      },
    ],
    policy_revision: 3,
  },
}

const issuedEnvelope = {
  data: {
    binding: { workspace_uid: WORKSPACE_UID },
    corpus_refs: ['nas-team-share'],
    expires_at: '2026-09-08T09:37:07Z',
    purpose: 'find_context',
    query: 'rollback verification owner',
    request_id: REQUEST_ID,
    requested_at: '2026-09-08T09:32:07Z',
    result_limit: 3,
    schema: 'workstack.knowledge-request.v1',
  },
  meta: {
    connection_alias: 'team-nas',
    policy_revision: 3,
    replayed: false,
    state: 'pending',
  },
}

const issueBody: KnowledgeIssueBody = {
  binding: { workspace_uid: WORKSPACE_UID },
  connection_alias: 'team-nas',
  corpus_refs: ['nas-team-share'],
  intent_id: INTENT_ID,
  purpose: 'find_context',
  query: 'rollback verification owner',
  result_limit: 3,
}

function stubFetch(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    return handler(url, init)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function headersOf(call: [RequestInfo | URL, (RequestInit | undefined)?]) {
  return (call[1]?.headers ?? {}) as Record<string, string>
}

test('reads the owner policy from the canonical route as a plain same-origin GET', async () => {
  const mock = stubFetch(async () => (await jsonResponse(policyEnvelope)))

  await expect(fetchKnowledgeConnections()).resolves.toEqual(policyEnvelope.data)

  const call = mock.mock.calls[0]
  expect(String(call[0])).toBe('/api/v1/knowledge/connections')
  expect(call[1]?.method).toBeUndefined()
  expect(call[1]?.credentials).toBe('same-origin')
  expect(headersOf(call)['X-WorkStack-CSRF']).toBeUndefined()
  // Nothing in this flow may reach anything but the loopback API surface.
  expect(mock.mock.calls.every(([input]) => String(input).startsWith('/api/v1/'))).toBe(true)
})

test('refuses a policy answer that grew a field the projection never promised', async () => {
  stubFetch(async () =>
    (await jsonResponse({
      data: {
        connections: [
          {
            alias: 'team-nas',
            corpus_refs: ['nas-team-share'],
            // A field the closed schema does not know. Reading it would widen the surface.
            endpoint: 'https://intranet.example.invalid/share',
            scope: 'workspace',
            upstream_workspace_uid: '66666666-6666-4666-8666-666666666666',
          },
        ],
        policy_revision: 1,
      },
    })),
  )

  await expect(fetchKnowledgeConnections()).rejects.toThrow()
})

test('replaces the policy with the closed CAS body, the CSRF header and no Idempotency-Key', async () => {
  const mock = stubFetch(async () => (await jsonResponse(policyEnvelope)))

  await expect(
    replaceKnowledgeConnections({
      connections: [
        {
          alias: 'team-nas',
          corpus_refs: ['nas-team-share', 'product-notes'],
          upstream_workspace_uid: '66666666-6666-4666-8666-666666666666',
        },
      ],
      expected_policy_revision: 2,
    }),
  ).resolves.toEqual(policyEnvelope.data)

  const call = mock.mock.calls.find(([input]) =>
    String(input).endsWith('/api/v1/knowledge/connections'),
  )!
  expect(call[1]?.method).toBe('POST')
  const headers = headersOf(call)
  expect(headers['X-WorkStack-CSRF']).toBe('csrf-token-for-test')
  expect(headers['Content-Type']).toBe('application/json')
  // The route refuses one, and it would cache a response body containing a query.
  expect(headers['Idempotency-Key']).toBeUndefined()
  expect(JSON.parse(String(call[1]?.body))).toEqual({
    connections: [
      {
        alias: 'team-nas',
        corpus_refs: ['nas-team-share', 'product-notes'],
        upstream_workspace_uid: '66666666-6666-4666-8666-666666666666',
      },
    ],
    expected_policy_revision: 2,
  })
})

test('issues a request with exactly the seven contract fields and returns the ledger facts', async () => {
  const mock = stubFetch(async () => (await jsonResponse(issuedEnvelope)))

  await expect(issueKnowledgeRequest(issueBody)).resolves.toEqual({
    meta: issuedEnvelope.meta,
    request: issuedEnvelope.data,
  })

  const call = mock.mock.calls.find(([input]) =>
    String(input).endsWith('/api/v1/knowledge/requests'),
  )!
  expect(headersOf(call)['Idempotency-Key']).toBeUndefined()
  const sent = JSON.parse(String(call[1]?.body)) as Record<string, unknown>
  expect(Object.keys(sent).sort()).toEqual([
    'binding',
    'connection_alias',
    'corpus_refs',
    'intent_id',
    'purpose',
    'query',
    'result_limit',
  ])
  expect(sent).toEqual(issueBody)
})

test('refuses an issued document that asserts a field the body may not carry', async () => {
  stubFetch(async () =>
    (await jsonResponse({
      data: { ...issuedEnvelope.data, authority: 'owner' },
      meta: issuedEnvelope.meta,
    })),
  )

  await expect(issueKnowledgeRequest(issueBody)).rejects.toThrow()
})

test('surfaces the unsupported backend refusal as its closed code, with no receipt', async () => {
  stubFetch(async () =>
    (await jsonResponse(
      {
        error: {
          code: 'knowledge_backend_unsupported',
          message: 'the knowledge ledger requires the collection store',
        },
      },
      409,
    )),
  )

  await expect(fetchKnowledgeConnections()).rejects.toMatchObject({
    code: 'knowledge_backend_unsupported',
    status: 409,
  })
  await expect(issueKnowledgeRequest(issueBody)).rejects.toBeInstanceOf(ApiError)
})

/**
 * The optional occupancy block. It is an observation attached to the answer the policy
 * already came in, so the tests below hold three things at once: the wire `data` schema
 * is untouched, a server that says nothing about occupancy is fully supported, and a
 * server that says something malformed costs the caller its usage display and nothing
 * else — never the policy, never the save.
 */

const occupancy = {
  byte_bound: 262144,
  encoded_bytes: 18432,
  request_bound: 200,
  request_count: 7,
} as const

const policyBody = {
  connections: [
    {
      alias: 'team-nas',
      corpus_refs: ['nas-team-share', 'product-notes'],
      upstream_workspace_uid: '66666666-6666-4666-8666-666666666666',
    },
  ],
  expected_policy_revision: 2,
}

function connectionCalls(mock: ReturnType<typeof stubFetch>) {
  return mock.mock.calls.filter(([input]) => String(input).endsWith('/knowledge/connections'))
}

test('attaches the validated occupancy the same read already answered with', async () => {
  const mock = stubFetch(async () => (await jsonResponse({ ...policyEnvelope, meta: { occupancy } })))

  await expect(fetchKnowledgeConnections()).resolves.toEqual({
    ...policyEnvelope.data,
    occupancy,
  })
  // One read. The observation costs no second request and no occupancy route.
  expect(connectionCalls(mock)).toHaveLength(1)
})

test('a server that publishes no meta leaves the policy exactly as it was', async () => {
  stubFetch(async () => (await jsonResponse(policyEnvelope)))

  const policy = await fetchKnowledgeConnections()

  expect(policy).toEqual(policyEnvelope.data)
  expect('occupancy' in policy).toBe(false)
})

test('every malformed occupancy is unavailable usage, never zero and never a read failure', async () => {
  const broken: unknown[] = [
    { ...occupancy, request_count: 201 }, // a count above its own bound
    { ...occupancy, encoded_bytes: 262145 }, // bytes above their own bound
    { ...occupancy, request_bound: 0 }, // a bound of zero bounds nothing
    { ...occupancy, byte_bound: -1 },
    { ...occupancy, encoded_bytes: 1.5 }, // not a whole number of bytes
    { ...occupancy, request_count: -1 },
    { ...occupancy, request_count: null }, // what a non-finite number becomes on the wire
    { ...occupancy, request_count: '7' },
    { ...occupancy, request_count: Number.MAX_SAFE_INTEGER + 2, request_bound: Number.MAX_SAFE_INTEGER + 2 },
    { ...occupancy, note: 'extra' }, // a key the closed block never published
    { byte_bound: 262144, encoded_bytes: 18432 }, // half the block
    'not an object',
    null,
  ]

  for (const value of broken) {
    stubFetch(async () => (await jsonResponse({ ...policyEnvelope, meta: { occupancy: value } })))
    const policy = await fetchKnowledgeConnections()
    // The policy beside it is perfectly valid and the owner still gets it.
    expect(policy).toEqual(policyEnvelope.data)
    expect(policy.occupancy).toBeUndefined()
  }

  // Meta that is not about occupancy at all reaches nothing either.
  stubFetch(async () =>
    (await jsonResponse({ ...policyEnvelope, meta: { audit_trail: 'unpublished' } })),
  )
  await expect(fetchKnowledgeConnections()).resolves.toEqual(policyEnvelope.data)
})

test('a policy save reports its own occupancy without changing the body it sent', async () => {
  const mock = stubFetch(async () => (await jsonResponse({ ...policyEnvelope, meta: { occupancy } })))

  await expect(replaceKnowledgeConnections(policyBody)).resolves.toEqual({
    ...policyEnvelope.data,
    occupancy,
  })

  const call = connectionCalls(mock).find(([, init]) => init?.method === 'POST')!
  // The closed CAS body, verbatim: no occupancy is ever sent.
  expect(JSON.parse(String(call[1]?.body))).toEqual(policyBody)
})

test('a malformed occupancy never turns an accepted policy save into a failure', async () => {
  stubFetch(async () =>
    (await jsonResponse({
      ...policyEnvelope,
      meta: { occupancy: { ...occupancy, request_count: 9999 } },
    })),
  )

  const saved = await replaceKnowledgeConnections(policyBody)

  expect(saved).toEqual(policyEnvelope.data)
  expect(saved.occupancy).toBeUndefined()
})
