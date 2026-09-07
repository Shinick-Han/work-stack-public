import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import { DailyReportDraftEditor } from './DailyReportDraftEditor'
import {
  REPORT_DRAFT_LOCK_NAME,
  REPORT_DRAFT_TEMPLATE,
  createReportDraftStore,
  type DraftStorage,
  type ReportDraft,
  type ReportDraftCoordinate,
  type ReportDraftErrorCode,
  type ReportDraftStore,
} from './reportDraftStorage'

/**
 * The editor is exercised against the REAL store over a fake in-memory storage,
 * so revision, compare-and-set and capacity behave exactly as they do in a
 * browser. Failure cases inject a store that returns one chosen code.
 *
 * Every fixture is synthetic. No live authority, network, clock or model.
 */

const WORKSPACE = '11111111-1111-4111-8111-111111111111'
const DIGEST = 'sha256:' + 'a'.repeat(64)
const OTHER_DIGEST = 'sha256:' + 'b'.repeat(64)
const GENERATED = '2026-09-06T09:00:00Z'
const LATER = '2026-09-06T11:30:00Z'
const SOURCE_MARKDOWN = '# Daily review\n\nGenerated body.\n'

const COORDINATE: ReportDraftCoordinate = {
  workspaceUid: WORKSPACE,
  date: '2026-09-06',
  template: REPORT_DRAFT_TEMPLATE,
}

function memoryStorage(): DraftStorage {
  const cells = new Map<string, string>()
  return {
    getItem: (key) => cells.get(key) ?? null,
    setItem: (key, value) => {
      cells.set(key, value)
    },
  }
}

/** The real store, so revisions and CAS are the production ones. */
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

function failingStore(
  code: ReportDraftErrorCode,
  operation: 'save' | 'remove' | 'load',
  inner: ReportDraftStore = realStore(),
): ReportDraftStore {
  return {
    load: (coordinate) =>
      operation === 'load'
        ? Promise.resolve({ ok: false as const, code })
        : inner.load(coordinate),
    save: (input, expected) =>
      operation === 'save'
        ? Promise.resolve({ ok: false as const, code })
        : inner.save(input, expected),
    remove: (coordinate, expected) =>
      operation === 'remove'
        ? Promise.resolve({ ok: false as const, code })
        : inner.remove(coordinate, expected),
  }
}

function renderEditor(
  overrides: Partial<React.ComponentProps<typeof DailyReportDraftEditor>> = {},
) {
  const onClose = vi.fn()
  const store = overrides.store ?? realStore()
  const view = render(
    <DailyReportDraftEditor
      coordinate={COORDINATE}
      source={{ sourceDigest: DIGEST, generatedAt: GENERATED, markdown: SOURCE_MARKDOWN }}
      onClose={onClose}
      store={store}
      {...overrides}
    />,
  )
  return { onClose, store, view }
}

function editor(): HTMLTextAreaElement {
  return screen.getByLabelText('Report markdown') as HTMLTextAreaElement
}

/**
 * Capture what a download would carry without letting jsdom attempt a real
 * navigation, which it does not implement and only reports as noise.
 */
function captureDownloads() {
  const blobs: Blob[] = []
  const names: string[] = []
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: (blob: Blob) => {
      blobs.push(blob)
      return 'blob:fixture'
    },
    revokeObjectURL: () => undefined,
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    names.push(this.download)
  })
  return { blobs, names }
}

async function settled() {
  await waitFor(() => expect(screen.queryByText('Opening the saved draft…')).toBeNull())
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('opening the editor', () => {
  test('an unsaved report opens on the generated markdown', async () => {
    renderEditor()
    await settled()
    expect(editor()).toHaveValue(SOURCE_MARKDOWN)
    expect(screen.getByText(/not saved on this device/)).toBeVisible()
    // The modal states the boundary rather than implying a published revision.
    expect(
      screen.getByText(/Local draft on this device only/),
    ).toBeVisible()
  })

  test('reopening shows the saved markdown, not the generated markdown', async () => {
    const user = userEvent.setup()
    const store = realStore()
    const first = renderEditor({ store })
    await settled()
    await user.clear(editor())
    await user.type(editor(), 'edited body')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible())
    // The saved record must carry the EDIT, not the markdown the editor opened on.
    const stored = await store.load(COORDINATE)
    expect(stored.ok && stored.value?.markdown).toBe('edited body')
    first.view.unmount()

    renderEditor({ store })
    await settled()
    expect(editor()).toHaveValue('edited body')
    expect(screen.getByText(/local revision 1/)).toBeVisible()
  })

  test('the first save is revision 1 and the next one advances it', async () => {
    const user = userEvent.setup()
    renderEditor()
    await settled()
    await user.type(editor(), 'one')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible())
    await user.type(editor(), ' two')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 2/)).toBeVisible())
  })
})

describe('a newer generated report never replaces edited text', () => {
  test('a changed source prop for the same coordinate keeps the text and marks it stale', async () => {
    const user = userEvent.setup()
    const { store, view } = renderEditor()
    await settled()
    await user.clear(editor())
    await user.type(editor(), 'my own words')

    // The SAME store, so the rerender cannot start a second session behind the
    // assertions below.
    view.rerender(
      <DailyReportDraftEditor
        coordinate={COORDINATE}
        source={{
          sourceDigest: OTHER_DIGEST,
          generatedAt: LATER,
          markdown: '# Regenerated\n\nDifferent body.\n',
        }}
        onClose={vi.fn()}
        store={store}
      />,
    )
    await settled()

    // The editor is not reset and the newer report is announced, not applied.
    expect(editor()).toHaveValue('my own words')
    expect(screen.getByText(/A newer report has been generated since/)).toBeVisible()
    expect(screen.getByText(new RegExp(GENERATED))).toBeVisible()
  })

  test('an identical source prop object identity change raises no notice', async () => {
    const { store, view } = renderEditor()
    await settled()
    view.rerender(
      <DailyReportDraftEditor
        coordinate={COORDINATE}
        source={{ sourceDigest: DIGEST, generatedAt: GENERATED, markdown: SOURCE_MARKDOWN }}
        onClose={vi.fn()}
        store={store}
      />,
    )
    await settled()
    expect(screen.queryByText(/A newer report has been generated since/)).toBeNull()
    expect(screen.queryByText(/generated again at/)).toBeNull()
  })

  test('the stale notice never points at a control that is unavailable', async () => {
    const { store, view } = renderEditor()
    await settled()
    // Nothing has been saved, so Delete is unavailable in this state.
    expect(screen.getByRole('button', { name: 'Delete saved draft' })).toBeDisabled()

    view.rerender(
      <DailyReportDraftEditor
        coordinate={COORDINATE}
        source={{ sourceDigest: OTHER_DIGEST, generatedAt: LATER, markdown: '# New report' }}
        onClose={vi.fn()}
        store={store}
      />,
    )
    await settled()

    const notice = screen.getByText(/A newer report has been generated since/)
    expect(notice).toBeVisible()
    // The instruction has to hold in this state too, so it names the fallbacks
    // and a plain discard rather than a Delete the reader cannot press.
    expect(notice).toHaveTextContent(/Copy Markdown or Download \.md/)
    expect(notice).not.toHaveTextContent(/delete the saved draft/i)
  })

  test('the same facts generated later are reported as current, not stale', async () => {
    const { store, view } = renderEditor()
    await settled()
    view.rerender(
      <DailyReportDraftEditor
        coordinate={COORDINATE}
        // Identical digest, later instant: the report did not change.
        source={{ sourceDigest: DIGEST, generatedAt: LATER, markdown: SOURCE_MARKDOWN }}
        onClose={vi.fn()}
        store={store}
      />,
    )
    await settled()
    expect(screen.queryByText(/A newer report has been generated since/)).toBeNull()
    expect(screen.getByText(/generated again at/)).toHaveTextContent(
      /not out of date/,
    )
  })
})

describe('save failures keep the text and name a fallback', () => {
  test.each([
    ['capacity', /no room left for another local draft/],
    ['conflict', /changed somewhere else on this device/],
    ['storage_unavailable', /Local storage is unavailable/],
    ['invalid_storage', /unreadable/],
  ] as const)('%s is explained and never erases the edit', async (code, expected) => {
    const user = userEvent.setup()
    renderEditor({ store: failingStore(code, 'save') })
    await settled()
    await user.clear(editor())
    await user.type(editor(), 'work I do not want to lose')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(expected))
    expect(editor()).toHaveValue('work I do not want to lose')
    // The fallback is offered rather than merely implied.
    expect(screen.getByRole('alert')).toHaveTextContent(/Copy Markdown or Download \.md/)
    expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled()
  })

  test('Copy Markdown copies the current edit, not the generated report', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    renderEditor({ store: failingStore('capacity', 'save') })
    await settled()
    await user.clear(editor())
    await user.type(editor(), 'current edit')
    await user.click(screen.getByRole('button', { name: 'Copy Markdown' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('current edit'))
  })
})

describe('closing and deleting are deliberate', () => {
  test('closing with unsaved text asks before discarding', async () => {
    const user = userEvent.setup()
    const { onClose } = renderEditor()
    await settled()
    await user.type(editor(), 'unsaved')
    await user.click(screen.getByRole('button', { name: 'Close' }))

    expect(onClose).not.toHaveBeenCalled()
    const prompt = screen.getByRole('alertdialog', { name: 'Discard unsaved changes' })
    expect(prompt).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(onClose).not.toHaveBeenCalled()
    expect(editor()).toHaveValue(SOURCE_MARKDOWN + 'unsaved')

    await user.click(screen.getByRole('button', { name: 'Close' }))
    await user.click(screen.getByRole('button', { name: 'Discard changes and close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  test('closing with no unsaved text closes immediately', async () => {
    const user = userEvent.setup()
    const { onClose } = renderEditor()
    await settled()
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  test('delete is unavailable until something is saved', async () => {
    renderEditor()
    await settled()
    expect(screen.getByRole('button', { name: 'Delete saved draft' })).toBeDisabled()
  })

  test('delete asks first, uses the loaded revision, and keeps the text', async () => {
    const user = userEvent.setup()
    const storage = memoryStorage()
    const inner = realStore(storage)
    const removals: number[] = []
    const store: ReportDraftStore = {
      load: inner.load,
      save: inner.save,
      remove: (coordinate, expected) => {
        removals.push(expected)
        return inner.remove(coordinate, expected)
      },
    }
    renderEditor({ store })
    await settled()
    await user.clear(editor())
    await user.type(editor(), 'saved then deleted')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible())

    await user.click(screen.getByRole('button', { name: 'Delete saved draft' }))
    expect(screen.getByRole('alertdialog', { name: 'Delete saved draft' })).toBeVisible()
    expect(removals).toEqual([])

    await user.click(screen.getByRole('button', { name: 'Delete the saved draft' }))
    await waitFor(() =>
      expect(screen.getByText(/The saved draft was deleted on this device/)).toBeVisible(),
    )
    // Compare-and-set against the revision the editor actually loaded.
    expect(removals).toEqual([1])
    // Deleting the saved copy is not discarding the text.
    expect(editor()).toHaveValue('saved then deleted')
    expect(screen.getByText(/not saved on this device/)).toBeVisible()
  })

  test('a refused delete says the text was untouched', async () => {
    const user = userEvent.setup()
    const storage = memoryStorage()
    const inner = realStore(storage)
    renderEditor({ store: failingStore('conflict', 'remove', inner) })
    await settled()
    await user.clear(editor())
    await user.type(editor(), 'still mine')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible())

    await user.click(screen.getByRole('button', { name: 'Delete saved draft' }))
    await user.click(screen.getByRole('button', { name: 'Delete the saved draft' }))
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(/text in the editor was not changed/),
    )
    expect(editor()).toHaveValue('still mine')
  })
})

describe('compare-and-set and unload guarding', () => {
  test('the first save expects null and the next expects the loaded revision', async () => {
    const user = userEvent.setup()
    const inner = realStore()
    const expectations: Array<number | null> = []
    const store: ReportDraftStore = {
      load: inner.load,
      save: (input, expected) => {
        expectations.push(expected)
        return inner.save(input, expected)
      },
      remove: inner.remove,
    }
    renderEditor({ store })
    await settled()
    await user.type(editor(), 'a')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible())
    await user.type(editor(), 'b')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 2/)).toBeVisible())
    expect(expectations).toEqual([null, 1])
  })

  test('a dirty editor guards the unload and a clean one does not', async () => {
    const user = userEvent.setup()
    const added = vi.spyOn(window, 'addEventListener')
    const removed = vi.spyOn(window, 'removeEventListener')
    renderEditor()
    await settled()
    expect(added.mock.calls.some(([name]) => name === 'beforeunload')).toBe(false)

    await user.type(editor(), 'x')
    await waitFor(() =>
      expect(added.mock.calls.some(([name]) => name === 'beforeunload')).toBe(true),
    )

    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible())
    // Saving makes the editor clean again, so the guard is released.
    await waitFor(() =>
      expect(removed.mock.calls.some(([name]) => name === 'beforeunload')).toBe(true),
    )
  })
})


describe('an ownership change releases the previous operation', () => {
  test.each(['save', 'remove'] as const)(
    'a pending %s for the old coordinate leaves the new editor usable',
    async (operation) => {
      const user = userEvent.setup()
      const inner = realStore()
      const store: ReportDraftStore = {
        load: inner.load,
        save: operation === 'save' ? () => new Promise(() => undefined) : inner.save,
        remove: operation === 'remove' ? () => new Promise(() => undefined) : inner.remove,
      }
      const { view } = renderEditor({ store })
      await settled()
      if (operation === 'remove') {
        // A remove needs something saved first, so this path saves through the
        // real store and only the removal hangs.
        await user.type(editor(), 'x')
        await user.click(screen.getByRole('button', { name: 'Save local draft' }))
        await waitFor(() =>
          expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible(),
        )
        await user.click(screen.getByRole('button', { name: 'Delete saved draft' }))
        await user.click(screen.getByRole('button', { name: 'Delete the saved draft' }))
      } else {
        await user.type(editor(), 'x')
        await user.click(screen.getByRole('button', { name: 'Save local draft' }))
      }
      // The operation never resolves; the editor moves to another day.
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Save local draft' })).toBeDisabled(),
      )

      view.rerender(
        <DailyReportDraftEditor
          coordinate={{ ...COORDINATE, date: '2026-09-07' }}
          source={{ sourceDigest: DIGEST, generatedAt: GENERATED, markdown: SOURCE_MARKDOWN }}
          onClose={vi.fn()}
          store={store}
        />,
      )
      await settled()

      // The new coordinate is usable: nothing is stuck busy and it can close.
      expect(screen.getByRole('button', { name: 'Save local draft' })).toBeEnabled()
      expect(screen.getByRole('button', { name: 'Close' })).toBeEnabled()
      expect(editor()).toBeEnabled()
    },
  )
})

describe('a confirmation actually blocks the editor beneath it', () => {
  async function promptDelete(store: ReportDraftStore) {
    const user = userEvent.setup()
    renderEditor({ store })
    await settled()
    await user.clear(editor())
    await user.type(editor(), 'saved once')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible())
    await user.click(screen.getByRole('button', { name: 'Delete saved draft' }))
    return user
  }

  test('the background editor and its actions are unavailable while prompting', async () => {
    await promptDelete(realStore())
    expect(screen.getByRole('alertdialog', { name: 'Delete saved draft' })).toBeVisible()
    // Not merely covered: the editing surface and every action are disabled.
    expect(editor()).toBeDisabled()
    for (const name of ['Save local draft', 'Copy Markdown', 'Download .md', 'Close']) {
      expect(screen.getByRole('button', { name })).toBeDisabled()
    }
    const body = document.querySelector('.report-draft-editor')
    expect(body?.getAttribute('inert')).not.toBeNull()
    expect(body?.getAttribute('aria-hidden')).toBe('true')
  })

  test('confirming acts only on the revision the prompt was raised against', async () => {
    const inner = realStore()
    const removals: number[] = []
    const store: ReportDraftStore = {
      load: inner.load,
      save: inner.save,
      remove: (coordinate, expected) => {
        removals.push(expected)
        return inner.remove(coordinate, expected)
      },
    }
    const user = await promptDelete(store)
    await user.click(screen.getByRole('button', { name: 'Delete the saved draft' }))
    await waitFor(() =>
      expect(screen.getByText(/The saved draft was deleted on this device/)).toBeVisible(),
    )
    expect(removals).toEqual([1])
    expect(editor()).toHaveValue('saved once')
  })
})

describe('deleting never costs the retained text', () => {
  test('an unchanged saved draft still prompts on close after a delete', async () => {
    const user = userEvent.setup()
    const store = realStore()
    // Saved markdown equal to the base it was started from: text equality alone
    // would read as clean the moment the saved copy disappeared.
    const { onClose, view } = renderEditor({ store })
    await settled()
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))
    await waitFor(() => expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible())

    view.rerender(
      <DailyReportDraftEditor
        coordinate={COORDINATE}
        source={{
          sourceDigest: OTHER_DIGEST,
          generatedAt: LATER,
          markdown: '# Regenerated\n',
        }}
        onClose={onClose}
        store={store}
      />,
    )
    await settled()

    await user.click(screen.getByRole('button', { name: 'Delete saved draft' }))
    await user.click(screen.getByRole('button', { name: 'Delete the saved draft' }))
    await waitFor(() =>
      expect(screen.getByText(/The saved draft was deleted on this device/)).toBeVisible(),
    )

    await user.click(screen.getByRole('button', { name: 'Close' }))
    // The text is unsaved now, so closing asks instead of dropping it.
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('alertdialog', { name: 'Discard unsaved changes' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(editor()).toHaveValue(SOURCE_MARKDOWN)

    // And both fallbacks still preserve it.
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    await user.click(screen.getByRole('button', { name: 'Copy Markdown' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SOURCE_MARKDOWN))

    const { blobs, names } = captureDownloads()
    await user.click(screen.getByRole('button', { name: 'Download .md' }))
    await waitFor(() =>
      expect(screen.getByText(/downloaded as a .md file/)).toBeVisible(),
    )
    expect(blobs).toHaveLength(1)
    expect(blobs[0].size).toBe(new TextEncoder().encode(SOURCE_MARKDOWN).length)
    expect(names).toEqual(['workstack-daily-2026-09-06-draft.md'])
  })
})

describe('closing paths all respect unsaved text', () => {
  test('Escape on the editor dialog asks before discarding', async () => {
    const user = userEvent.setup()
    const { onClose } = renderEditor()
    await settled()
    await user.type(editor(), 'typed')

    const dialog = document.querySelector('dialog.dialog') as HTMLDialogElement
    await act(async () => {
      dialog.dispatchEvent(new Event('cancel', { cancelable: true }))
    })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('alertdialog', { name: 'Discard unsaved changes' })).toBeVisible()
  })

  test('a backdrop click asks before discarding', async () => {
    const user = userEvent.setup()
    const { onClose } = renderEditor()
    await settled()
    await user.type(editor(), 'typed')

    const dialog = document.querySelector('dialog.dialog') as HTMLDialogElement
    await user.click(dialog)
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('alertdialog', { name: 'Discard unsaved changes' })).toBeVisible()
  })
})

describe('typing while a save is in flight', () => {
  test.each([true, false])(
    'keeps the newer text and stays unsaved (write succeeds: %s)',
    async (succeeds) => {
      const user = userEvent.setup()
      const inner = realStore()
      let release: (() => void) | null = null
      const store: ReportDraftStore = {
        load: inner.load,
        save: (input, expected) =>
          new Promise((resolve) => {
            release = () => {
              if (succeeds) void inner.save(input, expected).then(resolve)
              else resolve({ ok: false as const, code: 'capacity' })
            }
          }),
        remove: inner.remove,
      }
      renderEditor({ store })
      await settled()
      await user.clear(editor())
      await user.type(editor(), 'first')
      await user.click(screen.getByRole('button', { name: 'Save local draft' }))

      // The textarea is not locked during a save, so more can be typed.
      await user.type(editor(), ' second')
      await act(async () => {
        release?.()
        await Promise.resolve()
      })
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Save local draft' })).toBeEnabled(),
      )

      // The newer keystrokes survive either outcome and are still unsaved.
      expect(editor()).toHaveValue('first second')
      expect(screen.getByText(/unsaved changes/)).toBeVisible()
    },
  )
})

describe('keeping the text never waits on a write', () => {
  function hangingSave(succeeds: boolean) {
    const inner = realStore()
    let release: (() => void) | null = null
    const store: ReportDraftStore = {
      load: inner.load,
      save: (input, expected) =>
        new Promise((resolve) => {
          release = () => {
            if (succeeds) void inner.save(input, expected).then(resolve)
            else resolve({ ok: false as const, code: 'storage_write_failed' })
          }
        }),
      remove: inner.remove,
    }
    return { release: () => release?.(), store }
  }

  test.each([true, false])(
    'a hung save leaves Copy and Download usable and exporting the newest text (write succeeds: %s)',
    async (succeeds) => {
      const user = userEvent.setup()
      const writeText = vi.fn().mockResolvedValue(undefined)
      vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
      const { blobs } = captureDownloads()
      const { release, store } = hangingSave(succeeds)

      renderEditor({ store })
      await settled()
      await user.clear(editor())
      await user.type(editor(), 'first')
      await user.click(screen.getByRole('button', { name: 'Save local draft' }))

      // Writing is serialized, so Save and Delete wait.
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Save local draft' })).toBeDisabled(),
      )
      expect(screen.getByRole('button', { name: 'Delete saved draft' })).toBeDisabled()

      // Keeping the text is not: the textarea stays editable and both ways out
      // stay available while the store hangs.
      expect(editor()).toBeEnabled()
      await user.type(editor(), ' second')
      expect(screen.getByRole('button', { name: 'Copy Markdown' })).toBeEnabled()
      expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled()

      // And they export exactly what is on screen now, not the snapshot in flight.
      await user.click(screen.getByRole('button', { name: 'Copy Markdown' }))
      await waitFor(() => expect(writeText).toHaveBeenCalledWith('first second'))
      await user.click(screen.getByRole('button', { name: 'Download .md' }))
      await waitFor(() => expect(blobs).toHaveLength(1))
      expect(blobs[0].size).toBe(new TextEncoder().encode('first second').length)

      await act(async () => {
        release()
        await Promise.resolve()
      })
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Save local draft' })).toBeEnabled(),
      )

      // Resolving does not rewrite the newer text, and the outcome is reported.
      expect(editor()).toHaveValue('first second')
      expect(screen.getByText(/unsaved changes/)).toBeVisible()
      if (succeeds) {
        expect(screen.getByText(/Saved locally as revision 1/)).toBeVisible()
        expect(screen.getByRole('button', { name: 'Delete saved draft' })).toBeEnabled()
      } else {
        expect(screen.getByRole('alert')).toHaveTextContent(/refused the write/)
        expect(screen.getByRole('button', { name: 'Delete saved draft' })).toBeDisabled()
      }
    },
  )

  test('a prompt still owns every control, including the two fallbacks', async () => {
    const user = userEvent.setup()
    renderEditor()
    await settled()
    await user.type(editor(), 'typed')
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.getByRole('alertdialog', { name: 'Discard unsaved changes' })).toBeVisible()
    for (const name of ['Copy Markdown', 'Download .md']) {
      expect(screen.getByRole('button', { name })).toBeDisabled()
    }
  })
})

describe('owner-bound async work', () => {
  test('a load that resolves after unmount changes nothing', async () => {
    let release: (value: { ok: true; value: ReportDraft | null }) => void = () => undefined
    const store: ReportDraftStore = {
      load: () =>
        new Promise((resolve) => {
          release = resolve
        }),
      save: () => Promise.reject(new Error('unused')),
      remove: () => Promise.reject(new Error('unused')),
    }
    const { view } = renderEditor({ store })
    view.unmount()
    await act(async () => {
      release({ ok: true, value: null })
      await Promise.resolve()
    })
    // Nothing rendered, and no state update warning was raised.
    expect(screen.queryByLabelText('Report markdown')).toBeNull()
  })

  test('a save that resolves for a previous coordinate is ignored', async () => {
    const user = userEvent.setup()
    const pending: Array<(value: never) => void> = []
    const inner = realStore()
    const store: ReportDraftStore = {
      load: inner.load,
      save: () =>
        new Promise((resolve) => {
          pending.push(resolve as (value: never) => void)
        }),
      remove: inner.remove,
    }
    const { view } = renderEditor({ store })
    await settled()
    await user.type(editor(), 'first day')
    await user.click(screen.getByRole('button', { name: 'Save local draft' }))

    // The editor moves to another day before the first save resolves.
    view.rerender(
      <DailyReportDraftEditor
        coordinate={{ ...COORDINATE, date: '2026-09-07' }}
        source={{ sourceDigest: DIGEST, generatedAt: GENERATED, markdown: SOURCE_MARKDOWN }}
        onClose={vi.fn()}
        store={store}
      />,
    )
    await settled()
    await act(async () => {
      pending[0]?.({
        ok: true,
        value: { localRevision: 99 },
      } as never)
      await Promise.resolve()
    })
    // The stale result never claims a revision on the day now being edited.
    expect(screen.queryByText(/local revision 99/)).toBeNull()
    expect(screen.getByText(/not saved on this device/)).toBeVisible()
  })
})

describe('bounds and rendering', () => {
  test('the textarea is bounded and the reading view is plain text', async () => {
    const user = userEvent.setup()
    renderEditor()
    await settled()
    expect(editor().maxLength).toBe(100000)
    await user.click(screen.getByRole('button', { name: 'Show plain reading view' }))
    const reading = document.querySelector('.report-draft-editor__reading')
    expect(reading).not.toBeNull()
    expect(reading?.textContent).toBe(SOURCE_MARKDOWN)
    // Text nodes only: nothing in the draft became an element.
    expect(reading?.querySelector('*')).toBeNull()
  })

  test('markup in the draft is shown as characters, never parsed', async () => {
    const user = userEvent.setup()
    renderEditor()
    await settled()
    await user.clear(editor())
    await user.type(editor(), '<img src=x onerror=1>')
    await user.click(screen.getByRole('button', { name: 'Show plain reading view' }))
    const reading = document.querySelector('.report-draft-editor__reading')
    expect(reading?.textContent).toBe('<img src=x onerror=1>')
    expect(reading?.querySelector('img')).toBeNull()
  })
})
