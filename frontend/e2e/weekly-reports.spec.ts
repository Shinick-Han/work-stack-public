import { expect, test, type Page, type Response } from '@playwright/test'
import { readFile } from 'node:fs/promises'

/**
 * Weekly report browser oracle against the canonical fixture server.
 * Generate is explicit; clipboard/download match the live GET; owner
 * invalidation is observed through the product refetch paths.
 */

test.use({
  permissions: ['clipboard-read', 'clipboard-write'],
})

type WeeklyPreviewData = {
  workspace_uid: string
  source_digest: string
  preview: {
    template: string
    period: { start: string; end: string }
    markdown: string
  }
}

function clipboardComparable(text: string) {
  return text.replace(/\r\n/g, '\n')
}

function previousIsoDate(date: string) {
  const [year, month, day] = date.split('-').map(Number)
  const next = new Date(Date.UTC(year, month - 1, day))
  next.setUTCDate(next.getUTCDate() - 1)
  return next.toISOString().slice(0, 10)
}

function isPreviewGet(response: Response, kind: 'daily' | 'weekly') {
  if (response.request().method() !== 'GET') return false
  return response.url().includes(`/api/v1/reports/${kind}-preview`)
}

function isReviewGet(response: Response) {
  return response.request().method() === 'GET' && response.url().includes('/api/v1/review?')
}

function dailyReport(page: Page) {
  return page.getByRole('region', { name: 'Daily report', exact: true })
}

function weeklyReport(page: Page) {
  return page.getByRole('region', { name: 'Weekly report', exact: true })
}

async function gotoReview(page: Page) {
  await page.goto('/?surface=review')
  await expect(page.getByRole('heading', { name: 'Turn execution into evidence.' })).toBeVisible()
  await expect(weeklyReport(page).getByRole('button', { name: 'Generate weekly report', exact: true })).toBeVisible()
}

async function reviewDate(page: Page) {
  return page.getByRole('textbox', { name: 'Review date' }).inputValue()
}

async function workspaceUid(page: Page) {
  const response = await page.request.get('/api/v1/workspace')
  expect(response.ok(), await response.text()).toBeTruthy()
  const payload = await response.json() as { data: { workspace: { id: string } } }
  return payload.data.workspace.id
}

async function firstTaskId(page: Page) {
  const response = await page.request.get('/api/v1/workspace')
  expect(response.ok(), await response.text()).toBeTruthy()
  const payload = await response.json() as { data: { tasks: Array<{ id: string }> } }
  const taskId = payload.data.tasks[0]?.id
  expect(taskId).toBeTruthy()
  return taskId
}

async function postReviewEntry(page: Page, input: {
  date: string
  task_id: string
  done: string[]
  next: string[]
  blockers: string[]
}) {
  const session = await page.request.get('/api/v1/session')
  expect(session.ok()).toBeTruthy()
  const payload = await session.json() as { data: { csrf_token: string } }
  const origin = new URL(page.url()).origin
  const response = await page.request.post('/api/v1/review/entries', {
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      Origin: origin,
      'X-WorkStack-CSRF': payload.data.csrf_token,
      'Idempotency-Key': `e2e.weekly-worklog.${Date.now()}`,
    },
    data: input,
  })
  expect(response.ok(), await response.text()).toBeTruthy()
}

async function refetchReviewThroughProduct(page: Page) {
  const refresh = page.waitForResponse(isReviewGet)
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await refresh
}

async function generateWeekly(page: Page) {
  const pending = page.waitForResponse((item) => isPreviewGet(item, 'weekly'))
  await weeklyReport(page).getByRole('button', { name: 'Generate weekly report', exact: true }).click()
  const response = await pending
  expect(response.status()).toBe(200)
  const envelope = await response.json() as { data: WeeklyPreviewData }
  await expect(weeklyReport(page).locator('.daily-report-document')).toBeVisible()
  return envelope.data
}

async function generateDaily(page: Page) {
  const pending = page.waitForResponse((item) => isPreviewGet(item, 'daily'))
  await dailyReport(page).getByRole('button', { name: 'Generate report', exact: true }).click()
  const response = await pending
  expect(response.status()).toBe(200)
  await expect(dailyReport(page).locator('.daily-report-document')).toBeVisible()
}

async function measureWeeklyPanel(page: Page, viewportWidth: number) {
  return page.evaluate((width) => {
    const slack = 2
    const panel = document.querySelector<HTMLElement>('.weekly-report-preview')
    if (!panel) throw new Error('Weekly report panel is not mounted')
    const heading = panel.querySelector('h2')
    const bodyFont = getComputedStyle(document.body).fontFamily
    const panelFont = getComputedStyle(panel).fontFamily
    const headingFont = heading ? getComputedStyle(heading).fontFamily : ''
    const rect = panel.getBoundingClientRect()
    const within = (element: HTMLElement) => element.scrollWidth <= element.clientWidth + slack
    return {
      documentNoHorizontalScroll:
        document.documentElement.scrollWidth <= document.documentElement.clientWidth + slack,
      panelNoHorizontalScroll: within(panel),
      panelFitsViewport:
        rect.left >= -slack && rect.right <= width + slack && rect.width <= width + slack,
      bodyFont,
      panelInheritsProductFont: Boolean(panelFont) && panelFont === bodyFont,
      headingUsesProportionalProductFont:
        Boolean(headingFont) && headingFont === bodyFont && !/mono/i.test(headingFont),
    }
  }, viewportWidth)
}

test.beforeEach(async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
})

test('weekly generate copies and downloads the live GET markdown without a weekly editor', async ({ page }) => {
  let weeklyGets = 0
  page.on('request', (request) => {
    if (request.method() === 'GET' && request.url().includes('/api/v1/reports/weekly-preview')) {
      weeklyGets += 1
    }
  })
  await gotoReview(page)
  expect(weeklyGets).toBe(0)
  await expect(weeklyReport(page).locator('.daily-report-document')).toHaveCount(0)
  await expect(weeklyReport(page).getByRole('button', { name: 'Edit local draft' })).toHaveCount(0)

  const date = await reviewDate(page)
  const uid = await workspaceUid(page)
  const generated = await generateWeekly(page)
  expect(generated.workspace_uid).toBe(uid)
  expect(generated.preview.template).toBe('weekly-v1')
  expect(generated.preview.period.end).toBe(date)
  expect(generated.source_digest).toMatch(/^sha256:[0-9a-f]{64}$/)

  const query = new URLSearchParams({
    end_date: date,
    template: 'weekly-v1',
    workspace_uid: uid,
  })
  const independent = await page.request.get(`/api/v1/reports/weekly-preview?${query}`)
  expect(independent.ok(), await independent.text()).toBeTruthy()
  const independentBody = await independent.json() as { data: WeeklyPreviewData }
  expect(independentBody.data.source_digest).toBe(generated.source_digest)
  expect(independentBody.data.preview.period).toEqual(generated.preview.period)
  expect(weeklyGets).toBe(1)

  await expect(weeklyReport(page).getByRole('button', { name: 'Edit local draft' })).toHaveCount(0)
  await expect(page.getByRole('dialog', { name: 'Edit local report draft' })).toHaveCount(0)

  await weeklyReport(page).getByRole('button', { name: 'Copy Markdown', exact: true }).click()
  await expect(weeklyReport(page).getByRole('button', { name: 'Markdown copied', exact: true })).toBeVisible()
  const copied = await page.evaluate(() => navigator.clipboard.readText())
  expect(clipboardComparable(copied)).toBe(generated.preview.markdown)

  const downloadEvent = page.waitForEvent('download')
  await weeklyReport(page).getByRole('button', { name: 'Download .md', exact: true }).click()
  const download = await downloadEvent
  expect(download.suggestedFilename()).toBe(
    `workstack-weekly-${generated.preview.period.start}-to-${generated.preview.period.end}.md`,
  )
  const downloadPath = await download.path()
  expect(downloadPath).toBeTruthy()
  const downloaded = await readFile(downloadPath!)
  expect(downloaded.equals(Buffer.from(generated.preview.markdown, 'utf8'))).toBe(true)
  expect(weeklyGets).toBe(1)
})

test('unchanged refetch keeps both reports; check-in and D-1 worklog split daily from weekly', async ({ page }) => {
  let dailyGets = 0
  let weeklyGets = 0
  page.on('request', (request) => {
    if (request.method() !== 'GET') return
    if (request.url().includes('/api/v1/reports/daily-preview')) dailyGets += 1
    if (request.url().includes('/api/v1/reports/weekly-preview')) weeklyGets += 1
  })

  await gotoReview(page)
  await generateDaily(page)
  await generateWeekly(page)
  expect(dailyGets).toBe(1)
  expect(weeklyGets).toBe(1)
  await expect(dailyReport(page).locator('.daily-report-document')).toBeVisible()
  await expect(weeklyReport(page).locator('.daily-report-document')).toBeVisible()

  await refetchReviewThroughProduct(page)
  await expect(dailyReport(page).locator('.daily-report-document')).toBeVisible()
  await expect(weeklyReport(page).locator('.daily-report-document')).toBeVisible()
  expect(dailyGets).toBe(1)
  expect(weeklyGets).toBe(1)

  await page.getByRole('button', { name: 'Check in now', exact: true }).click()
  await expect(dailyReport(page).locator('.daily-report-document')).toHaveCount(0)
  await expect(weeklyReport(page).locator('.daily-report-document')).toBeVisible()
  expect(dailyGets).toBe(1)
  expect(weeklyGets).toBe(1)

  await generateDaily(page)
  expect(dailyGets).toBe(2)
  await expect(dailyReport(page).locator('.daily-report-document')).toBeVisible()

  const date = await reviewDate(page)
  const prior = previousIsoDate(date)
  const taskId = await firstTaskId(page)
  const afterFact = page.waitForResponse(isReviewGet)
  await postReviewEntry(page, {
    date: prior,
    task_id: taskId,
    done: [`Playwright weekly fact on ${prior}`],
    next: [],
    blockers: [],
  })
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await afterFact

  await expect(dailyReport(page).locator('.daily-report-document')).toBeVisible()
  await expect(weeklyReport(page).locator('.daily-report-document')).toHaveCount(0)
  await expect(weeklyReport(page).getByRole('button', { name: 'Generate weekly report', exact: true })).toBeVisible()
  expect(dailyGets).toBe(2)
  expect(weeklyGets).toBe(1)
})

test('weekly panel keeps product type and does not overflow at 1280 or 390', async ({ page }) => {
  await gotoReview(page)
  await generateWeekly(page)

  for (const viewport of [
    { width: 1280, height: 720 },
    { width: 390, height: 844 },
  ] as const) {
    await page.setViewportSize(viewport)
    await expect(weeklyReport(page)).toBeVisible()
    const fit = await measureWeeklyPanel(page, viewport.width)
    expect(fit.documentNoHorizontalScroll, `page must not overflow ${viewport.width}px`).toBe(true)
    expect(fit.panelNoHorizontalScroll, `weekly panel must not overflow ${viewport.width}px`).toBe(true)
    expect(fit.panelFitsViewport, `weekly panel must fit ${viewport.width}px viewport`).toBe(true)
    expect(fit.bodyFont.length).toBeGreaterThan(0)
    expect(fit.panelInheritsProductFont).toBe(true)
    expect(fit.headingUsesProportionalProductFont).toBe(true)
  }
})
