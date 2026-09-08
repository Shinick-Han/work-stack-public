import { expect, test, type Page, type Request } from '@playwright/test'
import { readFile } from 'node:fs/promises'

/**
 * Exercise report editing against the fixture server and real browser storage.
 * Local saves must preserve source facts, survive reopening, and detect writes
 * from another tab. Confirmation flows must retain unsaved text.
 */

test.use({
  permissions: ['clipboard-read', 'clipboard-write'],
})

const EDITED_MARKDOWN = '# local-draft-e2e\n\nedited-by-playwright-not-generated\n'
const WINNER_MARKDOWN = '# local-draft-e2e-winner\n\nfirst-tab-saved\n'
const SECOND_MARKDOWN = '# local-draft-e2e-second\n\nsecond-tab-still-mine\n'
const MUTATION_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

function editorDialog(page: Page) {
  return page.getByRole('dialog', { name: 'Edit local report draft' })
}

function markdownField(page: Page) {
  return editorDialog(page).getByLabel('Report markdown')
}

function discardPrompt(page: Page) {
  return page.getByRole('alertdialog', { name: 'Discard unsaved changes' })
}

function deletePrompt(page: Page) {
  return page.getByRole('alertdialog', { name: 'Delete saved draft' })
}

async function gotoReviewAndGenerate(page: Page) {
  await page.goto('/?surface=review')
  await expect(page.getByRole('heading', { name: 'Daily Review', exact: true })).toBeVisible()
  const reviewDate = await page.getByRole('textbox', { name: 'Review date' }).inputValue()
  await page.getByRole('button', { name: 'Generate report' }).click()
  await expect(page.getByRole('button', { name: 'Edit local draft' })).toBeVisible()
  return reviewDate
}

async function openEditor(page: Page) {
  await page.getByRole('button', { name: 'Edit local draft' }).click()
  await expect(editorDialog(page)).toBeVisible()
  await expect(markdownField(page)).toBeEnabled()
}

async function closeEditorWhenClean(page: Page) {
  await editorDialog(page).getByRole('button', { name: 'Close', exact: true }).click()
  await expect(editorDialog(page)).toBeHidden()
}

function clipboardComparable(text: string) {
  return text.replace(/\r\n/g, '\n')
}

function isBackendApi(url: string) {
  try {
    return new URL(url).pathname.startsWith('/api/')
  } catch {
    return false
  }
}

function trackBackendMutations(page: Page) {
  const mutations: string[] = []
  let armed = false
  const onRequest = (request: Request) => {
    if (!armed) return
    const method = request.method().toUpperCase()
    if (!MUTATION_METHODS.has(method)) return
    const url = request.url()
    if (!isBackendApi(url)) return
    mutations.push(`${method} ${new URL(url).pathname}`)
  }
  page.on('request', onRequest)
  return {
    start() {
      mutations.length = 0
      armed = true
    },
    stop() {
      armed = false
      return [...mutations]
    },
    dispose() {
      armed = false
      page.off('request', onRequest)
    },
  }
}

async function readClipboard(page: Page) {
  return page.evaluate(async () => navigator.clipboard.readText())
}

async function measureModalFit(page: Page, viewportWidth: number) {
  return page.evaluate((width) => {
    const dialog = document.querySelector<HTMLDialogElement>('dialog.dialog[open], dialog[open]')
    if (!dialog) throw new Error('Native editor <dialog> is not open')
    const surface = dialog.querySelector<HTMLElement>('.dialog__surface')
    const footer = dialog.querySelector<HTMLElement>('.dialog__footer')
    const editor = dialog.querySelector<HTMLElement>('.report-draft-editor')
    const textarea = dialog.querySelector<HTMLTextAreaElement>('textarea')
    const title = dialog.querySelector('h2')
    const slack = 2
    const rect = dialog.getBoundingClientRect()
    const within = (element: HTMLElement | null) => {
      if (!element) return true
      return element.scrollWidth <= element.clientWidth + slack
    }
    const bodyFont = getComputedStyle(document.body).fontFamily
    const editorFont = editor ? getComputedStyle(editor).fontFamily : ''
    const titleFont = title ? getComputedStyle(title).fontFamily : ''
    const textareaFont = textarea ? getComputedStyle(textarea).fontFamily : ''
    return {
      dialogFitsViewport:
        rect.left >= -slack && rect.right <= width + slack && rect.width <= width + slack,
      dialogNoHorizontalScroll: within(dialog),
      surfaceNoHorizontalScroll: within(surface),
      footerNoHorizontalScroll: within(footer),
      documentNoHorizontalScroll:
        document.documentElement.scrollWidth <= document.documentElement.clientWidth + slack,
      bodyFont,
      editorInheritsProductFont: Boolean(editorFont) && editorFont === bodyFont,
      titleUsesProportionalProductFont:
        Boolean(titleFont) && titleFont === bodyFont && !/mono/i.test(titleFont),
      textareaUsesMonospaceEditingFace: /mono|cascadia|consolas/i.test(textareaFont),
    }
  }, viewportWidth)
}

test.beforeEach(async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await gotoReviewAndGenerate(page)
  await openEditor(page)
})

test('generate → edit → save local draft retains the edit; copy/download are the edited bytes; save is not a backend mutation', async ({ page }) => {
  const original = await markdownField(page).inputValue()
  expect(original).not.toBe(EDITED_MARKDOWN)
  await markdownField(page).fill(EDITED_MARKDOWN)

  const tracker = trackBackendMutations(page)
  tracker.start()
  await editorDialog(page).getByRole('button', { name: 'Save local draft' }).click()
  await expect(editorDialog(page).getByText(/Saved locally as revision \d+/)).toBeVisible()
  const mutations = tracker.stop()
  tracker.dispose()
  expect(mutations, 'local draft save must not POST/PUT/PATCH/DELETE /api/*').toEqual([])

  await closeEditorWhenClean(page)
  const reviewDate = await page.getByRole('textbox', { name: 'Review date' }).inputValue()
  await openEditor(page)
  await expect(markdownField(page)).toHaveValue(EDITED_MARKDOWN)

  await editorDialog(page).getByRole('button', { name: 'Copy Markdown' }).click()
  await expect(editorDialog(page).getByText('The current text was copied as Markdown.')).toBeVisible()
  const copied = await readClipboard(page)
  expect(clipboardComparable(copied)).toBe(EDITED_MARKDOWN)
  expect(clipboardComparable(copied)).not.toBe(original)

  const downloadPromise = page.waitForEvent('download')
  await editorDialog(page).getByRole('button', { name: 'Download .md' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe(`workstack-daily-${reviewDate}-draft.md`)
  const downloadPath = await download.path()
  expect(downloadPath).toBeTruthy()
  const downloaded = await readFile(downloadPath!)
  expect(downloaded.equals(Buffer.from(EDITED_MARKDOWN, 'utf8'))).toBe(true)
  expect(downloaded.equals(Buffer.from(original, 'utf8'))).toBe(false)
})

test('two pages in one context: first save wins; second save conflicts without overwriting the second textarea or the persisted winner', async ({ page, context }) => {
  const page2 = await context.newPage()
  await gotoReviewAndGenerate(page2)
  await openEditor(page2)

  await markdownField(page).fill(WINNER_MARKDOWN)
  await markdownField(page2).fill(SECOND_MARKDOWN)
  expect(WINNER_MARKDOWN).not.toBe(SECOND_MARKDOWN)

  await editorDialog(page).getByRole('button', { name: 'Save local draft' }).click()
  await expect(editorDialog(page).getByText(/Saved locally as revision \d+/)).toBeVisible()

  await editorDialog(page2).getByRole('button', { name: 'Save local draft' }).click()
  await expect(editorDialog(page2).getByRole('alert')).toHaveText(/changed somewhere else on this device/)
  await expect(editorDialog(page2).getByRole('alert')).toHaveText(/Copy Markdown or Download \.md/)
  await expect(markdownField(page2)).toHaveValue(SECOND_MARKDOWN)
  await expect(markdownField(page)).toHaveValue(WINNER_MARKDOWN)

  await closeEditorWhenClean(page)
  await openEditor(page)
  await expect(markdownField(page)).toHaveValue(WINNER_MARKDOWN)
  await expect(markdownField(page2)).toHaveValue(SECOND_MARKDOWN)
  await page2.close()
})

test('dirty Close asks; Keep editing preserves; deleting a saved draft asks and does not discard the editor text; Copy Markdown still works', async ({ page }) => {
  const original = await markdownField(page).inputValue()
  await markdownField(page).fill(EDITED_MARKDOWN)

  await editorDialog(page).getByRole('button', { name: 'Close', exact: true }).click()
  await expect(discardPrompt(page)).toBeVisible()
  await expect(editorDialog(page)).toBeVisible()
  await discardPrompt(page).getByRole('button', { name: 'Keep editing' }).click()
  await expect(discardPrompt(page)).toBeHidden()
  await expect(markdownField(page)).toHaveValue(EDITED_MARKDOWN)

  await editorDialog(page).getByRole('button', { name: 'Save local draft' }).click()
  await expect(editorDialog(page).getByText(/Saved locally as revision \d+/)).toBeVisible()

  const deleteButton = editorDialog(page).getByRole('button', { name: 'Delete saved draft' })
  await expect(deleteButton).toBeEnabled()
  await deleteButton.click()
  await expect(deletePrompt(page)).toBeVisible()
  await deletePrompt(page).getByRole('button', { name: 'Keep editing' }).click()
  await expect(deletePrompt(page)).toBeHidden()
  await expect(markdownField(page)).toHaveValue(EDITED_MARKDOWN)
  await expect(deleteButton).toBeEnabled()

  await deleteButton.click()
  await deletePrompt(page).getByRole('button', { name: 'Delete the saved draft' }).click()
  await expect(editorDialog(page).getByText(/The saved draft was deleted on this device/)).toBeVisible()
  await expect(editorDialog(page).getByText(/not saved on this device/)).toBeVisible()
  await expect(markdownField(page)).toHaveValue(EDITED_MARKDOWN)
  expect(EDITED_MARKDOWN).not.toBe(original)

  await editorDialog(page).getByRole('button', { name: 'Copy Markdown' }).click()
  await expect(editorDialog(page).getByText('The current text was copied as Markdown.')).toBeVisible()
  expect(clipboardComparable(await readClipboard(page))).toBe(EDITED_MARKDOWN)
})

test('desktop and narrow viewports: native modal fits without horizontal overflow; type inherits the product face', async ({ page }) => {
  const viewports = [
    { width: 1440, height: 720 },
    { width: 390, height: 844 },
  ] as const

  for (const viewport of viewports) {
    await page.setViewportSize(viewport)
    await expect(editorDialog(page)).toBeVisible()
    const fit = await measureModalFit(page, viewport.width)
    expect(fit.dialogFitsViewport, `dialog must fit ${viewport.width}px viewport`).toBe(true)
    expect(fit.documentNoHorizontalScroll, `page must not overflow ${viewport.width}px`).toBe(true)
    expect(fit.dialogNoHorizontalScroll).toBe(true)
    expect(fit.surfaceNoHorizontalScroll).toBe(true)
    expect(fit.footerNoHorizontalScroll).toBe(true)
    expect(fit.bodyFont.length).toBeGreaterThan(0)
    expect(fit.editorInheritsProductFont).toBe(true)
    expect(fit.titleUsesProportionalProductFont).toBe(true)
    expect(fit.textareaUsesMonospaceEditingFace).toBe(true)
  }
})
