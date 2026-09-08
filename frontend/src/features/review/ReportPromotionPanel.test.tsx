import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import { jsonResponse } from '../../test/fixtures'
import { getCsrfToken } from '../../api/transport'
import { REPORT_IDEMPOTENCY_KEY } from '../../domain/reportDocuments'
import { DailyReportDraftEditor } from './DailyReportDraftEditor'
import {
  REPORT_DRAFT_LOCK_NAME,
  REPORT_DRAFT_TEMPLATE,
  createReportDraftStore,
  type DraftStorage,
  type ReportDraftCoordinate,
  type ReportDraftStore,
} from './reportDraftStorage'

/**
 * Saving the edited draft to the workspace, over the REAL client and the real
 * transport with only `fetch` replaced. Everything the panel promises is checked
 * against the bytes that actually leave: the route, the pinned identity, the
 * base digest, the frozen text and the one key that names the request.
 *
 * The local draft is exercised against the real store over in-memory storage, so
 * "the local draft was not touched" is a fact about the store, not a claim.
 *
 * Every fixture is synthetic. No live authority, network, clock or model.
 */

const WORKSPACE = '11111111-1111-4111-8111-111111111111'
const REPORT_UID = '22222222-2222-4222-8222-222222222222'
const DIGEST = 'sha256:' + 'a'.repeat(64)
const NEWER_DIGEST = 'sha256:' + 'b'.repeat(64)
const GENERATED = '2026-09-06T09:00:00Z'
const NEWER_GENERATED = '2026-09-06T15:00:00Z'
const MOMENT = '2026-09-06T12:00:00Z'
const SOURCE_MARKDOWN = '# Daily review\n\nGenerated body.\n'
const DATE = '2026-09-06'
const OTHER_DATE = '2026-09-07'
const CREATE_URL = `/api/v1/reports?workspace_uid=${WORKSPACE}`

const COORDINATE: ReportDraftCoordinate = {
  workspaceUid: WORKSPACE,
  date: DATE,
  template: REPORT_DRAFT_TEMPLATE,
}

const SOURCE = { sourceDigest: DIGEST, generatedAt: GENERATED, markdown: SOURCE_MARKDOWN }

function memoryStorage(): DraftStorage {
  const cells = new Map<string, string>()
  return {
    getItem: (key) => cells.get(key) ?? null,
    setItem: (key, value) => {
      cells.set(key, value)
    },
  }
}

function realStore(storage: DraftStorage = memoryStorage()): ReportDraftStore {
  return createReportDraftStore({
    storage: () => storage,
    withExclusiveLock: async (name, run) => {
      expect(name).toBe(REPORT_DRAFT_LOCK_NAME)
      return run()
    },
    now: () => new Date('2026-09-06T12:00:00.000Z'),
  })
}

/** A create response that answers the exact markdown the request carried. */
function created(markdown: string, date = DATE) {
  return {
    uid: REPORT_UID,
    workspace_uid: WORKSPACE,
    template: 'daily-v1',
    period: { kind: 'day', date },
    source_digest: DIGEST,
    source_generated_at: GENERATED,
    state: 'draft',
    revision: 1,
    archived_from_state: null,
    archived_at: null,
    archive_note: null,
    created_at: MOMENT,
    updated_at: MOMENT,
    content_entry: {
      content_revision: 1,
      document_revision: 1,
      markdown,
      authored_at: MOMENT,
      note: null,
    },
    source_stale: false,
  }
}

function jsonBody(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function refusal(code: string, status = 409) {
  return jsonResponse({ error: { code, message: 'refused' } }, status)
}

type Handler = (url: string, init?: RequestInit) => Promise<Response> | Response

function sessionAnd(handler: Handler) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'csrf-token-for-test' } })
    }
    return handler(url, init)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

interface Post {
  body: Record<string, unknown>
  key: string | undefined
  url: string
}

function posts(fetchMock: ReturnType<typeof vi.fn>): Post[] {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === 'POST')
    .map(([input, init]) => {
      const request = init as RequestInit
      const headers = request.headers as Record<string, string>
      return {
        body: JSON.parse(String(request.body)) as Record<string, unknown>,
        key: headers['Idempotency-Key'],
        url: String(input),
      }
    })
}

function renderEditor(overrides: Partial<React.ComponentProps<typeof DailyReportDraftEditor>> = {}) {
  const onClose = vi.fn()
  const store = overrides.store ?? realStore()
  const view = render(
    <DailyReportDraftEditor
      coordinate={COORDINATE}
      onClose={onClose}
      source={SOURCE}
      store={store}
      {...overrides}
    />,
  )
  return { onClose, store, view }
}

function editor(): HTMLTextAreaElement {
  return screen.getByLabelText('Report markdown') as HTMLTextAreaElement
}

function promote(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Save report to workspace' }) as HTMLButtonElement
}

async function settled() {
  await waitFor(() => expect(screen.queryByText('Opening the saved draft…')).toBeNull())
  await waitFor(() => expect(promote()).toBeEnabled())
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('asking the workspace to hold the report', () => {
  test('sends nothing until the reader asks, then the pinned identity, the base digest and the text on screen', async () => {
    const user = userEvent.setup()
    const fetchMock = sessionAnd((url, init) => {
      if (url !== CREATE_URL) throw new Error(`Unexpected request: ${url}`)
      const body = JSON.parse(String(init?.body)) as { markdown: string }
      return jsonResponse({ data: created(body.markdown), meta: { replayed: false } }, 201)
    })
    await getCsrfToken(true)

    renderEditor()
    await settled()
    // Rendering, loading and showing the panel are not a save.
    expect(posts(fetchMock)).toHaveLength(0)

    await user.type(editor(), ' edited')
    const expected = SOURCE_MARKDOWN + ' edited'
    await user.click(promote())

    await screen.findByText(/Saved to your workspace as report version 1/)
    const sent = posts(fetchMock)
    expect(sent).toHaveLength(1)
    expect(sent[0].url).toBe(CREATE_URL)
    expect(sent[0].body).toEqual({
      workspace_uid: WORKSPACE,
      template: 'daily-v1',
      period: { kind: 'day', date: DATE },
      source_digest: DIGEST,
      source_generated_at: GENERATED,
      markdown: expected,
    })
    expect(sent[0].key).toMatch(REPORT_IDEMPOTENCY_KEY)

    // The version shown is the server's content version, and it is named as a
    // draft there. The local draft was never written, moved or removed.
    expect(screen.getByText(/It is a draft there and is not finalized/)).toBeVisible()
    expect(editor()).toHaveValue(expected)
    expect(screen.getByText(/not saved on this device/)).toBeVisible()
  })

  test('the local save and the workspace save are separate actions, and neither is the other', async () => {
    const user = userEvent.setup()
    const fetchMock = sessionAnd((url, init) => {
      if (url !== CREATE_URL) throw new Error(`Unexpected request: ${url}`)
      const body = JSON.parse(String(init?.body)) as { markdown: string }
      return jsonResponse({ data: created(body.markdown), meta: { replayed: false } }, 201)
    })
    await getCsrfToken(true)

    renderEditor()
    await settled()

    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await screen.findByText(/Saved locally as revision 1/)
    // Saving locally reaches no network at all.
    expect(posts(fetchMock)).toHaveLength(0)

    await user.click(promote())
    await screen.findByText(/Saved to your workspace as report version 1/)
    // The workspace save leaves the local revision exactly where it was.
    expect(screen.getByText(/local revision 1/)).toBeVisible()
    expect(posts(fetchMock)[0].body).toMatchObject({ markdown: SOURCE_MARKDOWN })
  })
})

describe('text typed while a save is in flight', () => {
  test('belongs to the next report, not to the one already asked for', async () => {
    const user = userEvent.setup()
    let answer: (response: Response) => void = () => undefined
    const held = new Promise<Response>((resolve) => {
      answer = resolve
    })
    const fetchMock = sessionAnd((url) => {
      if (url !== CREATE_URL) throw new Error(`Unexpected request: ${url}`)
      return held
    })
    await getCsrfToken(true)

    renderEditor()
    await settled()
    await user.click(promote())
    await screen.findByText('Saving to the workspace…')

    await user.type(editor(), ' typed later')
    const frozen = posts(fetchMock)[0].body.markdown as string
    expect(frozen).toBe(SOURCE_MARKDOWN)

    await act(async () => {
      answer(jsonBody({ data: created(frozen), meta: { replayed: false } }, 201))
      await Promise.resolve()
    })

    await screen.findByText(/Saved to your workspace as report version 1/)
    // The newer text is still on screen and still unsaved; the report that was
    // saved carries the snapshot the reader asked for.
    expect(editor()).toHaveValue(SOURCE_MARKDOWN + ' typed later')
    expect(screen.getByText(/unsaved changes/)).toBeVisible()
    expect(posts(fetchMock)).toHaveLength(1)
  })
})

describe('an answer that never arrived', () => {
  test('is settled by asking the identical question again, never by a request built from newer text', async () => {
    const user = userEvent.setup()
    let live = false
    const fetchMock = sessionAnd((url, init) => {
      if (url !== CREATE_URL) throw new Error(`Unexpected request: ${url}`)
      if (!live) return Promise.reject(new TypeError('Failed to fetch'))
      const body = JSON.parse(String(init?.body)) as { markdown: string }
      return jsonResponse({ data: created(body.markdown), meta: { replayed: false } }, 201)
    })
    await getCsrfToken(true)

    renderEditor()
    await settled()
    await user.click(promote())

    await screen.findByText(/may or may not have been saved there/)
    // A second, differently-keyed save is exactly how a report gets saved twice,
    // so the only offer is the same question again.
    expect(promote()).toBeDisabled()
    const retry = screen.getByRole('button', { name: 'Ask the workspace again' })
    expect(retry).toBeEnabled()
    // The fallbacks are untouched by a failure of any kind.
    expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled()

    await user.type(editor(), ' typed after the failure')
    live = true
    await user.click(retry)

    await screen.findByText(/Saved to your workspace as report version 1/)
    const sent = posts(fetchMock)
    // The transport's own network retry plus the reader's retry: every one of
    // them is the same immutable operation under the same key.
    expect(sent.length).toBeGreaterThanOrEqual(2)
    for (const post of sent) {
      expect(post.body).toEqual(sent[0].body)
      expect(post.key).toBe(sent[0].key)
    }
    expect(sent[0].body.markdown).toBe(SOURCE_MARKDOWN)
    expect(editor()).toHaveValue(SOURCE_MARKDOWN + ' typed after the failure')
  })
})

describe('refusals the workspace is certain about', () => {
  test('a day already covered is explained, changes nothing, and reuses the same key when asked again', async () => {
    const user = userEvent.setup()
    const fetchMock = sessionAnd((url) => {
      if (url !== CREATE_URL) throw new Error(`Unexpected request: ${url}`)
      return refusal('report_duplicate_period')
    })
    await getCsrfToken(true)

    renderEditor()
    await settled()
    await user.click(promote())

    await screen.findByText(/A report for this day is already saved in this workspace/)
    expect(screen.getByText(/left exactly as it is and nothing new was saved/)).toBeVisible()
    expect(screen.getByText(/Your text is still here/)).toBeVisible()
    expect(editor()).toHaveValue(SOURCE_MARKDOWN)
    expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled()

    // The same unchanged report, asked for again, is the same request — a fresh
    // key would let the workspace treat it as a second report.
    await waitFor(() => expect(promote()).toBeEnabled())
    await user.click(promote())
    await waitFor(() => expect(posts(fetchMock)).toHaveLength(2))
    const sent = posts(fetchMock)
    expect(sent[1].key).toBe(sent[0].key)
    expect(sent[1].body).toEqual(sent[0].body)

    // The local draft still works, and it works on the text that was kept.
    await waitFor(() => expect(promote()).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await screen.findByText(/Saved locally as revision 1/)
  })

  test('a full workspace is named as full, with no remedy the workspace does not actually offer', async () => {
    const user = userEvent.setup()
    const fetchMock = sessionAnd((url) => {
      if (url !== CREATE_URL) throw new Error(`Unexpected request: ${url}`)
      return refusal('report_document_limit')
    })
    await getCsrfToken(true)

    renderEditor()
    await settled()
    await user.click(promote())

    await screen.findByText(/reached the most saved reports it can hold/)
    // The workspace counts archived reports toward the same bound and offers no
    // way to remove one, so nothing on screen may send the reader to archive or
    // delete something in the hope of making room.
    expect(screen.queryByText(/archive a saved report/i)).toBeNull()
    expect(screen.queryByText(/delete a saved report/i)).toBeNull()
    expect(screen.getByText(/Archived reports still count toward that limit/)).toBeVisible()
    expect(screen.getByText(/asking again will be refused the same way/)).toBeVisible()

    // Nothing was saved and nothing was lost: the text stands untouched and both
    // ways of keeping it off this workspace are still there.
    expect(editor()).toHaveValue(SOURCE_MARKDOWN)
    expect(screen.getByText(/Your text is still here/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled()
    expect(posts(fetchMock)).toHaveLength(1)
  })

  test('a changed source is reported without adopting it, and the newer report has to be read first', async () => {
    const user = userEvent.setup()
    const fetchMock = sessionAnd((url) => {
      if (url !== CREATE_URL) throw new Error(`Unexpected request: ${url}`)
      return refusal('report_source_changed')
    })
    await getCsrfToken(true)

    const { store, view } = renderEditor()
    await settled()
    await user.click(promote())

    await screen.findByText(/no longer the current one, so nothing was saved/)
    expect(screen.getByText(/Read the current report first and save from that/)).toBeVisible()
    expect(editor()).toHaveValue(SOURCE_MARKDOWN)

    // The newer report arrives. The panel reports it and stops offering the save;
    // it does not quietly re-send the reader's prose under the new digest.
    view.rerender(
      <DailyReportDraftEditor
        coordinate={COORDINATE}
        onClose={() => undefined}
        source={{
          sourceDigest: NEWER_DIGEST,
          generatedAt: NEWER_GENERATED,
          markdown: '# Newer\n',
        }}
        store={store}
      />,
    )
    await waitFor(() => expect(promote()).toBeDisabled())
    expect(
      screen.getByText(/A newer daily report has been generated since this draft started/),
    ).toBeVisible()
    expect(editor()).toHaveValue(SOURCE_MARKDOWN)
    expect(posts(fetchMock)).toHaveLength(1)
    expect(posts(fetchMock)[0].body.source_digest).toBe(DIGEST)
  })
})

describe('a result that no longer belongs to the panel', () => {
  test('is dropped after the reader has moved to another day and back', async () => {
    const user = userEvent.setup()
    let answer: (response: Response) => void = () => undefined
    const held = new Promise<Response>((resolve) => {
      answer = resolve
    })
    const fetchMock = sessionAnd((url) => {
      if (url !== CREATE_URL) throw new Error(`Unexpected request: ${url}`)
      return held
    })
    await getCsrfToken(true)

    const store = realStore()
    const { view } = renderEditor({ store })
    await settled()
    await user.click(promote())
    await screen.findByText('Saving to the workspace…')

    const show = (date: string) =>
      view.rerender(
        <DailyReportDraftEditor
          coordinate={{ ...COORDINATE, date }}
          onClose={() => undefined}
          source={SOURCE}
          store={store}
        />,
      )

    show(OTHER_DATE)
    await settled()
    show(DATE)
    await settled()

    await act(async () => {
      answer(jsonBody({ data: created(SOURCE_MARKDOWN), meta: { replayed: false } }, 201))
      await Promise.resolve()
    })

    // The day it was asked for is on screen again, but this is a THIRD session.
    // The first session's answer speaks for none of them.
    expect(screen.queryByText(/Saved to your workspace as report version/)).toBeNull()
    expect(screen.queryByText('Saving to the workspace…')).toBeNull()
    expect(promote()).toBeEnabled()
    expect(posts(fetchMock)).toHaveLength(1)
  })
})
