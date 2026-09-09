import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import { captureSchema } from '../../domain/schemaSourceCaptureReply'
import { EvidencePanel } from './EvidencePanel'
import { knowledgeCapture, loadOwnedR6ProjectionFixture, retrievalProjection } from './knowledgeCaptureFixture'
import { capture, jsonResponse, workspace } from '../../test/fixtures'
import {
  KNOWLEDGE_CAPTURE_OBSERVATION_PATH,
  KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH,
} from '../../api/knowledgeObservation'
import type { Capture } from '../../domain/types'

test('1.0 captures stay manual and unverified with no source actions', () => {
  const { container } = render(<EvidencePanel capture={capture} />)
  expect(screen.getByText('Manual import / unverified source')).toBeInTheDocument()
  expect(screen.getByText('No retrieval evidence is stored on this capture.')).toBeInTheDocument()
  expect(container.querySelectorAll('a, button')).toHaveLength(0)
})

test('1.1 evidence shows reported claims, score disclaimer, and version states', () => {
  render(<EvidencePanel capture={knowledgeCapture({
    retrieval: retrievalProjection({ truncated: true }),
  })} />)
  expect(screen.getByText('Manual import / unverified source')).toBeInTheDocument()
  expect(screen.getByText('Reported Notion page (claim)')).toBeInTheDocument()
  expect(screen.getByText('Version reported, unverified')).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Capture evidence' })).toHaveTextContent('not a correctness probability')
  expect(screen.getByText(/truncated/i)).toBeInTheDocument()
  expect(screen.queryByText(/OpenDocuments verified/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/verified current/i)).not.toBeInTheDocument()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})

test('verified_* version states are not presented as implemented verification', () => {
  render(<EvidencePanel capture={knowledgeCapture({
    retrieval: retrievalProjection({
      origin: { document_ref: 'od-page-7f3ba1d34f50c884600112ab', source_type: 'notion.page' },
      origin_state: 'verified',
      evidence: [{
        reported_source_type: 'notion.page',
        title: 'Release quality gate',
        document_ref: 'od-page-7f3ba1d34f50c884600112ab',
        chunk_ref: 'chunk-0004abcd',
        reported_source_version: 'od-version-14',
        version_state: 'verified_stale',
        indexed_digest: `sha256:${'a'.repeat(64)}`,
        web_url: null,
      }],
    }),
  })} />)
  expect(screen.getByText('Version reported, unverified')).toBeInTheDocument()
  expect(screen.queryByText(/stale/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/offline/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/revoked/i)).not.toBeInTheDocument()
})

test('owned R6 listed single_source and synthesized records render as unverified claims', () => {
  const fixture = loadOwnedR6ProjectionFixture()
  const listedSingle = captureSchema.parse(fixture.listed[0]) as Capture
  const { rerender } = render(<EvidencePanel capture={listedSingle} />)
  expect(screen.getByText('Reported NAS file (claim)')).toBeInTheDocument()
  expect(screen.getByText('Version reported, unverified')).toBeInTheDocument()
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  expect(screen.queryByText(/OpenDocuments verified/i)).not.toBeInTheDocument()

  rerender(<EvidencePanel capture={captureSchema.parse(fixture.listed[1]) as Capture} />)
  expect(screen.getByText('Reported Notion page (claim)')).toBeInTheDocument()
  expect(screen.getByText('Version unreported')).toBeInTheDocument()
  expect(screen.getByText('Manual import / unverified source')).toBeInTheDocument()
})

const WORKSPACE_UID = workspace.workspace.id
const DOC_A = 'od-page-7f3ba1d34f50c884600112ab'
const DOC_B = 'od-file-2c19aa4471bd0e33f1005511'

/** Two rows with different expected facts, so a positional binding check has real work. */
const twoSourceRetrieval = () => retrievalProjection({
  answer_scope: 'synthesized',
  origin_state: 'synthesized',
  reported_origin: null,
  evidence: [
    retrievalProjection().evidence[0],
    {
      reported_source_type: 'nas.file',
      title: 'Rollback checklist',
      document_ref: DOC_B,
      chunk_ref: null,
      reported_source_version: null,
      version_state: 'unreported',
      indexed_digest: null,
      web_url: null,
    },
  ],
})

/** The exact R21 success envelope for the two rows above. */
function verifyEnvelope(statuses: Array<Record<string, unknown>>) {
  return {
    data: {
      binding: { workspace_uid: WORKSPACE_UID, capture_id: 'C-0001', capture_revision: 0 },
      result: {
        schema: 'workstack.knowledge-verification.v1',
        verification_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
        checked_at: '2026-09-09T10:15:02Z',
        evidence: [
          {
            document_ref: DOC_A,
            source_type: 'notion.page',
            expected_source_version: 'od-version-14',
            observed_source_version: null,
            status: 'unverifiable',
            code: 'verification_unavailable',
            ...statuses[0],
          },
          {
            document_ref: DOC_B,
            source_type: 'nas.file',
            expected_source_version: null,
            observed_source_version: null,
            status: 'unverifiable',
            code: 'no_expected_version',
            ...statuses[1],
          },
        ],
      },
    },
    meta: { outcome: 'verification_ready' },
  }
}

/**
 * An owner from before the saved-check routes existed: the history read is answered with
 * a plain `not_found` 404, which is the one answer R27-E accepts as an old-server
 * diagnosis, and the panel keeps R21's transient check. The tests below therefore still
 * describe the old surface — as legacy behaviour, which is what it now is.
 */
function stubVerify(respond: () => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const target = String(input)
    if (target.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    if (target.startsWith(KNOWLEDGE_CAPTURE_OBSERVATION_PATH)) {
      return jsonResponse({ error: { code: 'not_found', message: 'no route' } }, 404)
    }
    return respond()
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

/** An owner that publishes the saved-check routes. */
function stubSaved(observation: unknown, record?: () => Promise<Response>) {
  const mock = vi.fn((input: RequestInfo | URL) => {
    const target = String(input)
    if (target.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    if (target.startsWith(KNOWLEDGE_CAPTURE_OBSERVATION_PATH)) {
      return jsonResponse(observationEnvelope(observation, 'observation_ready'))
    }
    if (target.startsWith(KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)) {
      return record ? record() : jsonResponse(observationEnvelope(observation, 'observation_recorded'))
    }
    throw new Error(`Unexpected request: ${target}`)
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

function observationEnvelope(observation: unknown, outcome: string) {
  return {
    data: {
      binding: { workspace_uid: WORKSPACE_UID, capture_id: 'C-0001', capture_revision: 0 },
      observation,
    },
    meta: { outcome },
  }
}

/** A saved check whose evidence lines up with the two rows `renderCheckable` shows. */
function savedUnchanged(statuses: Array<Record<string, unknown>> = [{}, {}]) {
  const checkedAt = '2026-09-08T10:15:02Z'
  return {
    accepted_at: '2026-09-08T10:15:04Z',
    checked_at: checkedAt,
    binding_state: 'unchanged',
    result: { ...verifyEnvelope(statuses).data.result, checked_at: checkedAt },
  }
}

function routeCalls(mock: ReturnType<typeof vi.fn>, path: string) {
  return mock.mock.calls.filter(([input]: unknown[]) => String(input).startsWith(path))
}

const recordCalls = (mock: ReturnType<typeof vi.fn>) => routeCalls(mock, KNOWLEDGE_CAPTURE_RECORD_CHECK_PATH)
const verifyCalls = (mock: ReturnType<typeof vi.fn>) => routeCalls(mock, '/api/v1/knowledge/captures/verify')

const SAVED_CHANGED = {
  accepted_at: '2026-09-08T10:15:04Z',
  checked_at: '2026-09-08T10:15:02Z',
  binding_state: 'changed',
  result: null,
}

function renderCheckable() {
  return render(
    <EvidencePanel
      capture={knowledgeCapture({ retrieval: twoSourceRetrieval() })}
      workspaceUid={WORKSPACE_UID}
    />,
  )
}

afterEach(() => { vi.unstubAllGlobals() })

test('a 1.0 capture offers no check even when a workspace UID is supplied', () => {
  const mock = stubVerify(async () => await jsonResponse({}))
  const { container } = render(<EvidencePanel capture={capture} workspaceUid={WORKSPACE_UID} />)
  expect(screen.queryByRole('button', { name: /check source status/i })).not.toBeInTheDocument()
  expect(container.querySelectorAll('a, button')).toHaveLength(0)
  expect(mock).not.toHaveBeenCalled()
})

test('an explicit check posts the exact contract body and shows each observed status beside the claims', async () => {
  const mock = stubVerify(async () => await jsonResponse(verifyEnvelope([
    { observed_source_version: 'od-version-19', status: 'stale', code: 'hash_differs' },
    { status: 'missing', code: 'file_absent' },
  ])))
  renderCheckable()
  // The history read is the only automatic request; the check itself still waits.
  await screen.findByText('Saved checks are unavailable on this server.')
  expect(mock.mock.calls.filter(([input]) => String(input).endsWith('/captures/verify'))).toHaveLength(0)

  await userEvent.click(screen.getByRole('button', { name: 'Check source status' }))
  expect(await screen.findByText('Source changed')).toBeInTheDocument()
  expect(screen.getByText('File not found')).toBeInTheDocument()
  expect(screen.getByText(/^Checked /)).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Capture evidence' }))
    .toHaveTextContent('does not update the saved capture')

  const post = mock.mock.calls.find(([input]) => String(input).endsWith('/captures/verify'))
  expect(JSON.parse(String(post?.[1]?.body))).toEqual({
    workspace_uid: WORKSPACE_UID,
    capture_id: 'C-0001',
    capture_revision: 0,
  })

  // The reported claims survive the observation: nothing is relabelled as verified.
  expect(screen.getByText('Reported Notion page (claim)')).toBeInTheDocument()
  expect(screen.getByText('Version reported, unverified')).toBeInTheDocument()
  expect(screen.getByText('Version unreported')).toBeInTheDocument()
  expect(screen.getByText('Manual import / unverified source')).toBeInTheDocument()
})

test('a checked panel shows no raw JSON, protocol name, opaque ref or version string', async () => {
  stubVerify(async () => await jsonResponse(verifyEnvelope([
    { observed_source_version: 'od-version-14', status: 'current', code: 'hash_matched' },
    { status: 'denied', code: 'access_denied' },
  ])))
  const { container } = renderCheckable()
  await screen.findByText('Saved checks are unavailable on this server.')
  await userEvent.click(screen.getByRole('button', { name: 'Check source status' }))
  expect(await screen.findByText('Matched when checked')).toBeInTheDocument()
  expect(screen.getByText('Access denied')).toBeInTheDocument()

  const shown = container.textContent ?? ''
  for (const hidden of [
    DOC_A, DOC_B, 'od-version-14', 'od-version-19', 'hash_matched', 'access_denied',
    'workstack.knowledge-verification.v1', 'cccccccc', 'verification_ready', '{', '}',
  ]) {
    expect(shown).not.toContain(hidden)
  }
})

test('a refused check reports closed copy and never the backend message', async () => {
  stubVerify(async () => await jsonResponse(
    { error: { code: 'verification_authority_changed', message: 'policy 7 -> 8 for alias team-nas' } },
    409,
  ))
  renderCheckable()
  await screen.findByText('Saved checks are unavailable on this server.')
  await userEvent.click(screen.getByRole('button', { name: 'Check source status' }))
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('Access to this source changed.')
  expect(alert).not.toHaveTextContent('team-nas')
  expect(screen.queryByText(/^Checked /)).not.toBeInTheDocument()
  expect(screen.queryByText('Matched when checked')).not.toBeInTheDocument()
})

test('a widened success is refused rather than shown as a successful check', async () => {
  // current/hash_matched with no observed version is not a legal pair.
  stubVerify(async () => await jsonResponse(verifyEnvelope([{ status: 'current', code: 'hash_matched' }, {}])))
  renderCheckable()
  await screen.findByText('Saved checks are unavailable on this server.')
  await userEvent.click(screen.getByRole('button', { name: 'Check source status' }))
  expect(await screen.findByRole('alert'))
    .toHaveTextContent('The source check answer could not be read')
  expect(screen.queryByText('Matched when checked')).not.toBeInTheDocument()
})

/*
 * R27-E: the saved source check.
 *
 * These panels talk to an owner that publishes the history routes. The reader sees what
 * was kept, in the past tense, and a check is recorded only when they ask for one.
 */

test('a saved check reads on open and shows each saved status beside the claims', async () => {
  const mock = stubSaved(savedUnchanged([
    { observed_source_version: 'od-version-19', status: 'stale', code: 'hash_differs' },
    { status: 'missing', code: 'file_absent' },
  ]))
  renderCheckable()

  expect(await screen.findByText(/^Last checked /)).toBeInTheDocument()
  expect(screen.getByText('Source changed')).toBeInTheDocument()
  expect(screen.getByText('File not found')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Check and save source status' })).toBeEnabled()

  // The read is a read. Nothing was recorded, and the old route was not consulted.
  expect(recordCalls(mock)).toHaveLength(0)
  expect(verifyCalls(mock)).toHaveLength(0)

  // The standing note says what a saved check is, and the stored claims are untouched.
  expect(screen.getByRole('region', { name: 'Capture evidence' }))
    .toHaveTextContent('does not show the sources as they are now')
  expect(screen.getByText('Reported Notion page (claim)')).toBeInTheDocument()
  expect(screen.getByText('Version reported, unverified')).toBeInTheDocument()
  expect(screen.getByText('Manual import / unverified source')).toBeInTheDocument()
  expect(screen.queryByText('Saved checks are unavailable on this server.')).not.toBeInTheDocument()
})

test('an empty history says so and claims nothing about what was written', async () => {
  stubSaved(null)
  renderCheckable()
  expect(await screen.findByText('No saved source check.')).toBeInTheDocument()
  expect(screen.queryByText(/^Last checked /)).not.toBeInTheDocument()
  expect(screen.queryByText('Matched when checked')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Check and save source status' })).toBeEnabled()
})

test('a changed binding shows the time and the changed sentence, never the old statuses', async () => {
  stubSaved(SAVED_CHANGED)
  renderCheckable()
  expect(await screen.findByText('Sources or access settings changed since this check.')).toBeInTheDocument()
  expect(screen.getByText(/^Last checked /)).toBeInTheDocument()
  for (const label of ['Matched when checked', 'Source changed', 'File not found', 'Access denied']) {
    expect(screen.queryByText(label)).not.toBeInTheDocument()
  }
})

test('one explicit click records one check and the panel then shows what was saved', async () => {
  const mock = stubSaved(savedUnchanged([
    { observed_source_version: 'od-version-14', status: 'current', code: 'hash_matched' },
    { status: 'denied', code: 'access_denied' },
  ]))
  renderCheckable()
  await screen.findByText(/^Last checked /)

  await userEvent.click(screen.getByRole('button', { name: 'Check and save source status' }))
  expect(await screen.findByText('Matched when checked')).toBeInTheDocument()
  expect(screen.getByText('Access denied')).toBeInTheDocument()

  const posts = recordCalls(mock)
  expect(posts).toHaveLength(1)
  expect(JSON.parse(String((posts[0][1] as RequestInit).body))).toEqual({
    workspace_uid: WORKSPACE_UID,
    capture_id: 'C-0001',
    capture_revision: 0,
  })
})

test('a failed record clears the statuses, shows closed copy and waits for an explicit reload', async () => {
  const mock = stubSaved(savedUnchanged(), async () => await jsonResponse(
    { error: { code: 'observation_save_unknown', message: 'OSError 28 on /srv/store.db' } },
    503,
  ))
  renderCheckable()
  await screen.findByText(/^Last checked /)

  await userEvent.click(screen.getByRole('button', { name: 'Check and save source status' }))
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('could not be confirmed')
  expect(alert).not.toHaveTextContent('OSError')
  expect(screen.queryByText('Matched when checked')).not.toBeInTheDocument()
  expect(screen.queryByText(/^Last checked /)).not.toBeInTheDocument()
  expect(screen.queryByText('No saved source check.')).not.toBeInTheDocument()

  // Recording again is shut until the reader reloads; nothing repeated itself meanwhile.
  expect(screen.getByRole('button', { name: 'Check and save source status' })).toBeDisabled()
  expect(recordCalls(mock)).toHaveLength(1)
  expect(verifyCalls(mock)).toHaveLength(0)

  await userEvent.click(screen.getByRole('button', { name: 'Reload saved check' }))
  expect(await screen.findByText(/^Last checked /)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Check and save source status' })).toBeEnabled()
})

test('an undetermined history offers no check and no old-server fallback', async () => {
  const mock = vi.fn((input: RequestInfo | URL) => {
    const target = String(input)
    if (target.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    if (target.startsWith(KNOWLEDGE_CAPTURE_OBSERVATION_PATH)) {
      return jsonResponse({ error: { code: 'observation_read_unavailable', message: '/srv/store.db' } }, 503)
    }
    throw new Error(`Unexpected request: ${target}`)
  })
  vi.stubGlobal('fetch', mock)
  renderCheckable()

  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('Saved source checks could not be loaded from this server.')
  expect(alert).not.toHaveTextContent('store.db')
  // A 503 is not evidence about the server's age: the old readonly check is not offered.
  expect(screen.queryByRole('button', { name: 'Check source status' })).not.toBeInTheDocument()
  expect(screen.queryByText('Saved checks are unavailable on this server.')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Check and save source status' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Reload saved check' })).toBeEnabled()
  expect(screen.queryByText('No saved source check.')).not.toBeInTheDocument()
})

test('a saved panel shows no raw JSON, protocol name, opaque ref, version string or wire time', async () => {
  stubSaved(savedUnchanged([
    { observed_source_version: 'od-version-14', status: 'current', code: 'hash_matched' },
    { status: 'revoked', code: 'mapping_revoked' },
  ]))
  const { container } = renderCheckable()
  expect(await screen.findByText('Matched when checked')).toBeInTheDocument()

  const shown = container.textContent ?? ''
  for (const hidden of [
    DOC_A, DOC_B, 'od-version-14', 'od-version-19', 'hash_matched', 'mapping_revoked',
    'workstack.knowledge-verification.v1', 'cccccccc', 'observation_ready', 'observation_recorded',
    'binding_state', '2026-09-08T10:15:02Z', '{', '}',
  ]) {
    expect(shown).not.toContain(hidden)
  }
})
