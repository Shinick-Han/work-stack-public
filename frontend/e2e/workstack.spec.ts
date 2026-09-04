import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Locator, type Page } from '@playwright/test'

type ThemeName = 'dark' | 'light'

async function useTheme(page: Page, theme: ThemeName) {
  const current = await page.locator('html').getAttribute('data-theme')
  if (current !== theme) {
    await page.getByRole('button', { name: `Use ${theme} theme` }).click()
  }
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
}

async function renderedThemeStyle(locator: Locator) {
  return locator.evaluate((element) => {
    const style = window.getComputedStyle(element)
    return {
      backgroundColor: style.backgroundColor,
      backgroundImage: style.backgroundImage,
      borderColor: style.borderColor,
      color: style.color,
    }
  })
}

type RenderedThemeStyle = Awaited<ReturnType<typeof renderedThemeStyle>>

async function waitForRenderedThemeChange(
  locator: Locator,
  previous: RenderedThemeStyle,
  properties: Array<keyof RenderedThemeStyle>,
) {
  for (const property of properties) {
    await expect.poll(
      async () => (await renderedThemeStyle(locator))[property],
      { message: `${property} should follow the selected product theme` },
    ).not.toBe(previous[property])
  }
  return renderedThemeStyle(locator)
}

function relativeLuminance(hexColor: string) {
  const hex = hexColor.trim().replace(/^#/, '')
  if (!/^[0-9a-f]{6}$/i.test(hex)) throw new Error(`Expected an opaque six-digit color, received ${hexColor}`)
  const channels = [0, 2, 4].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255)
  const [red, green, blue] = channels.map((channel) => (
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  ))
  return (0.2126 * red) + (0.7152 * green) + (0.0722 * blue)
}

function contrastRatio(first: string, second: string) {
  const firstLuminance = relativeLuminance(first)
  const secondLuminance = relativeLuminance(second)
  return (Math.max(firstLuminance, secondLuminance) + 0.05)
    / (Math.min(firstLuminance, secondLuminance) + 0.05)
}

async function semanticTokenContrast(page: Page, foreground: string, background: string) {
  const values = await page.locator('html').evaluate((element, properties) => {
    const style = window.getComputedStyle(element)
    return properties.map((property) => style.getPropertyValue(property).trim())
  }, [foreground, background])
  return contrastRatio(values[0], values[1])
}

test.beforeEach(async ({ page }) => {
  await page.goto('/?view=board')
  await expect(page.getByRole('heading', { name: 'Keep execution connected to intent.' })).toBeVisible()
})

test('the same Board Task click opens and then clears the shared selection', async ({ page }) => {
  const card = page.locator('article[aria-label^="T-0001:"]')
  await expect(card).toBeVisible()
  await expect(card.getByLabel('Steps for T-0001: 1 of 2 done')).toBeVisible()

  await card.click()
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeVisible()
  await expect(card).toHaveClass(/is-selected/)
  await expect(page).toHaveURL(/task=T-0001/)

  await card.click()
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeHidden()
  await expect(card).not.toHaveClass(/is-selected/)
  await expect(page).not.toHaveURL(/task=/)
})

test('the collapsed sidebar Task navigator searches and opens a Task in Workspace', async ({ page }) => {
  const taskSearch = page.getByRole('searchbox', { name: 'Filter sidebar tasks' })
  await expect(taskSearch).toBeHidden()

  await page.getByRole('button', { name: 'Tasks 30' }).click()
  await expect(taskSearch).toBeVisible()
  await taskSearch.fill('metric ownership')
  await page.getByRole('button', { name: 'Open task T-0021: Define metric ownership' }).click()

  await expect(page.getByRole('complementary', { name: 'Task T-0021' })).toBeVisible()
  await expect(page).toHaveURL(/view=board.*task=T-0021/)
})

test('sidebar section controls remain visible while only the Task list scrolls', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 720 })
  const disclosure = page.getByRole('button', { name: 'Tasks 30' })
  await disclosure.click()
  const taskSearch = page.getByRole('searchbox', { name: 'Filter sidebar tasks' })
  const openTable = page.getByRole('button', { name: 'Open Table' })
  const taskList = page.locator('.sidebar-tasks .task-nav')

  await taskList.evaluate((element) => element.scrollTo({ top: element.scrollHeight }))
  await expect.poll(() => taskList.evaluate((element) => element.scrollTop)).toBeGreaterThan(0)
  await expect(disclosure).toBeVisible()
  await expect(openTable).toBeVisible()
  await expect(taskSearch).toBeVisible()
  expect(await page.locator('.sidebar-tasks').evaluate((element) => element.scrollTop)).toBe(0)
})

test('Quick Add creates one Task and opens its authoritative drawer', async ({ page }) => {
  await page.getByRole('button', { name: 'New task' }).click()
  await page.getByRole('textbox', { name: 'Task title' }).fill('Browser smoke intent')
  await page.getByRole('button', { name: 'Create task' }).click()

  await expect(page.getByRole('complementary', { name: 'Task T-0031' })).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Task title' })).toHaveValue('Browser smoke intent')
  await expect(page).toHaveURL(/task=T-0031/)
})

test('Workspace actions downloads an explicitly confirmed verified local backup', async ({ page }) => {
  await page.getByRole('button', { name: 'More workspace actions' }).click()
  const dialog = page.getByRole('dialog', { name: 'Workspace actions' })
  await expect(dialog.getByText(/Work Stack \d+\.\d+\.\d+/)).toBeVisible()
  await expect(dialog.getByText(/Ready · schema/)).toBeVisible()
  await expect(dialog.getByText('Manual, verified updates')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Copy safe support summary' })).toBeVisible()
  const backupButton = dialog.getByRole('button', { name: 'Download verified backup' })
  await expect(backupButton).toBeDisabled()
  await dialog.getByText('I understand this file contains the full local workspace.').click()
  const downloadPromise = page.waitForEvent('download')
  await backupButton.click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/^workstack-backup-[0-9TZ]+-[0-9a-f]{8}\.zip$/)
  await expect(page.getByText('Verified local backup download started.')).toBeVisible()
})

test('Daily Review records Task evidence and updates its deterministic roll-up', async ({ page }) => {
  await page.goto('/?surface=review')
  await expect(page.getByRole('heading', { name: 'Turn execution into evidence.' })).toBeVisible()
  await page.getByLabel(/Done/).fill('Playwright verified the review loop')
  await page.getByLabel(/Next/).fill('Continue product maturation')
  await page.getByRole('button', { name: 'Add review entry' }).click()

  await expect(page.getByText('Daily review entry added')).toBeVisible()
  await expect(page.getByText('Playwright verified the review loop')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Seven-day review' })).toBeVisible()
})

test('Objective Hub adds and updates a measurable Key Result', async ({ page }) => {
  await page.goto('/?surface=objectives&objective=O-1')
  await expect(page.getByRole('heading', { name: 'Make the goal–work chain explicit.' })).toBeVisible()
  await page.getByLabel('New Key Result').fill('Playwright objective signal')
  await page.getByLabel('Target label').fill('100% verified')
  await page.getByRole('button', { name: 'Add Key Result' }).click()

  await expect(page.getByText('Key Result added')).toBeVisible()
  const row = page.locator('.kr-row').last()
  await expect(row).toBeVisible()
  await expect(row.getByLabel('Key Result description')).toHaveValue('Playwright objective signal')
  await row.getByLabel('Key Result target').fill('95% verified')
  await row.getByRole('slider').fill('35')
  await row.getByRole('button', { name: 'Save KR' }).click()
  await expect(page.getByText('Key Result updated')).toBeVisible()
  await expect(row.getByText('35%')).toBeVisible()

  const quarter = page.getByLabel('Objective quarter')
  const nextQuarter = await quarter.inputValue() === '2026-Q4' ? '2026-Q3' : '2026-Q4'
  await quarter.fill(nextQuarter)
  await page.getByRole('button', { name: 'Save Objective' }).click()
  await expect(page.getByText('Objective updated')).toBeVisible()
  await expect(quarter).toHaveValue(nextQuarter)
})

test('Objective Hub creates and selects a new idempotent Objective', async ({ page }) => {
  await page.goto('/?surface=objectives')
  await page.getByRole('button', { name: 'New Objective' }).click()
  await page.getByLabel('New Objective title').fill('Playwright direct objective')
  await page.getByLabel('New Objective quarter').fill('2026-Q4')
  await page.getByRole('button', { name: 'Create Objective' }).click()

  await expect(page.getByText(/Objective O-[0-9]+ added/)).toBeVisible()
  await expect(page.getByLabel('Objective title')).toHaveValue('Playwright direct objective')
  await expect(page).toHaveURL(/objective=O-[0-9]+/)
})

test('Objective Hub opens Quick Add with explicit alignment preselected', async ({ page }) => {
  await page.goto('/?surface=objectives&objective=O-1')
  await page.getByRole('button', { name: 'Create aligned task' }).click()

  const dialog = page.getByRole('dialog', { name: 'New task' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel(/Objective/)).toHaveValue('O-1')
  await dialog.getByRole('button', { name: 'Close', exact: true }).click()
})

test('Objective Hub scrolls inside a viewport-pinned shell', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 520 })
  await page.goto('/?surface=objectives&objective=O-1')
  await expect(page.getByRole('heading', { name: 'Make the goal–work chain explicit.' })).toBeVisible()

  const layout = await page.evaluate(() => {
    const measure = (selector: string) => {
      const element = document.querySelector<HTMLElement>(selector)
      if (!element) throw new Error(`Missing ${selector}`)
      const bounds = element.getBoundingClientRect()
      return {
        bottom: bounds.bottom,
        clientHeight: element.clientHeight,
        height: bounds.height,
        scrollHeight: element.scrollHeight,
      }
    }
    return {
      main: measure('.app-main'),
      shell: measure('.app-shell'),
      sidebar: measure('.app-sidebar'),
      sidebarFooter: measure('.sidebar-local'),
      stage: measure('.app-stage'),
    }
  })

  expect(layout.shell.height).toBe(520)
  expect(layout.sidebar.height).toBe(520)
  expect(layout.stage.height).toBe(520)
  expect(layout.sidebarFooter.bottom).toBe(520)
  expect(layout.main.scrollHeight).toBeGreaterThan(layout.main.clientHeight)

  const main = page.locator('.app-main')
  await main.evaluate((element) => element.scrollTo({ top: element.scrollHeight }))
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBeGreaterThan(0)
})

test('Objective Hub explains execution readiness and opens a blocked aligned Task', async ({ page }) => {
  await page.goto('/?surface=objectives&objective=O-4')
  const readiness = page.getByLabel('Objective execution readiness')
  await expect(readiness).toBeVisible()
  const blockedTask = page.locator('.objective-task-list button').filter({ has: page.locator('strong', { hasText: 'T-0019' }) })
  await expect(blockedTask.getByText('blocked')).toBeVisible()
  await expect(blockedTask.getByText('Waiting on T-0012')).toBeVisible()
  await blockedTask.click()
  await expect(page.getByRole('complementary', { name: 'Task T-0019' })).toBeVisible()
})

test('Task detail opens its aligned Objective without changing planning state', async ({ page }) => {
  await page.goto('/?view=table&task=T-0001')
  const drawer = page.getByRole('complementary', { name: 'Task T-0001' })
  await expect(drawer).toBeVisible()
  await drawer.getByRole('button', { name: 'Open objective O-1' }).click()

  await expect(page.getByRole('heading', { name: 'Make the goal–work chain explicit.' })).toBeVisible()
  await expect(page).toHaveURL(/surface=objectives/)
  await expect(page).toHaveURL(/objective=O-1/)
  await expect(page).not.toHaveURL(/task=/)
})

test('Task detail follows an existing dependency without a planning mutation', async ({ page }) => {
  await page.goto('/?view=table&task=T-0024')
  const drawer = page.getByRole('complementary', { name: 'Task T-0024' })
  await expect(drawer).toBeVisible()
  await drawer.getByRole('button', { name: 'Open dependency T-0019' }).click()

  const dependencyDrawer = page.getByRole('complementary', { name: 'Task T-0019' })
  await expect(dependencyDrawer).toBeVisible()
  await expect(page).toHaveURL(/task=T-0019/)
  await expect(dependencyDrawer.getByRole('button', { name: 'Open child T-0023' })).toBeVisible()
  await dependencyDrawer.getByRole('button', { name: 'Open dependent T-0024' }).click()
  await expect(drawer).toBeVisible()
  await expect(page).toHaveURL(/task=T-0024/)
})

test('Task detail excludes cyclic choices and edits dependencies from Task options', async ({ page }) => {
  await page.goto('/?view=table&task=T-0019')
  const drawer = page.getByRole('complementary', { name: 'Task T-0019' })
  const parent = drawer.getByLabel('Parent')
  const addDependency = drawer.getByLabel('Add dependency')
  await expect(parent).toHaveValue('')
  await expect(parent.locator('option[value="T-0023"]')).toHaveCount(0)
  await expect(addDependency.locator('option[value="T-0024"]')).toHaveCount(0)

  await addDependency.selectOption('T-0001')

  const removeDependency = drawer.getByRole('button', { name: 'Remove dependency T-0001' })
  await expect(removeDependency).toBeEnabled()
  await removeDependency.click()
  await expect(removeDependency).toBeHidden()
  await expect(drawer.getByText('Saved')).toBeVisible()
})

test('Task detail cannot close across an invalid unsaved title without explicit discard', async ({ page }) => {
  await page.goto('/?view=table')
  const taskRow = page.getByRole('row').filter({ hasText: 'T-0024' })
  await taskRow.getByText('T-0024', { exact: true }).click()
  const drawer = page.getByRole('complementary', { name: 'Task T-0024' })
  const title = drawer.getByRole('textbox', { name: 'Task title' })
  await expect(title).toHaveValue('Publish insight review')
  await title.fill('')
  await drawer.getByRole('button', { name: 'Close task drawer' }).click()

  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('alert')).toContainText('Task title cannot be empty')
  await page.goBack()
  await expect(page).toHaveURL(/task=T-0024/)
  await expect(drawer).toBeVisible()
  await drawer.getByRole('button', { name: 'Discard unsaved changes' }).click()
  await expect(title).toHaveValue('Publish insight review')
  await drawer.getByRole('button', { name: 'Close task drawer' }).click()
  await expect(drawer).toBeHidden()
})

test('Table View keeps objective focus in Workspace and unified search opens Objective Hub', async ({ page }) => {
  await page.goto('/?view=table')
  const taskRow = page.locator('tbody tr').filter({
    has: page.locator('td:first-child strong').filter({ hasText: /^T-0001$/ }),
  })
  await expect(taskRow).toBeVisible()
  await expect(taskRow.getByLabel('Steps for T-0001: 1 of 2 done')).toBeVisible()
  await taskRow.getByRole('button', { name: 'Focus objective O-1' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Keep execution connected to intent.' })).toBeVisible()
  await expect(page).toHaveURL(/objective=O-1/)
  await expect(page).not.toHaveURL(/surface=objectives/)

  await page.goto('/?view=table')
  const restoredTaskRow = page.locator('tbody tr').filter({
    has: page.locator('td:first-child strong').filter({ hasText: /^T-0001$/ }),
  })

  await restoredTaskRow.getByText('T-0001', { exact: true }).click()
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeVisible()
  await restoredTaskRow.getByText('T-0001', { exact: true }).click()
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeHidden()

  await page.getByRole('button', { name: /Search or jump/ }).click()
  await page.getByRole('searchbox', { name: 'Search commands and workspace' }).fill('Release quality')
  const objectiveResult = page.getByRole('dialog').getByRole('option', { name: /O-1.*Release quality/ })
  await expect(objectiveResult).toBeVisible()
  await objectiveResult.click()

  await expect(page.getByRole('heading', { name: 'Make the goal–work chain explicit.' })).toBeVisible()
  await expect(page).toHaveURL(/surface=objectives/)
})

test('Table density persists and narrow screens prioritize core planning columns', async ({ page }) => {
  await page.goto('/?view=table')
  await page.getByRole('button', { name: 'Compact rows' }).click()
  await expect(page.getByRole('button', { name: 'Compact rows' })).toHaveAttribute('aria-pressed', 'true')

  await page.reload()
  await expect(page.getByRole('button', { name: 'Compact rows' })).toHaveAttribute('aria-pressed', 'true')

  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('columnheader', { name: /Task/ })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: /Status/ })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: /ID/ })).toBeHidden()
  await expect(page.getByRole('columnheader', { name: /Context/ })).toBeHidden()
  await expect(page.getByRole('columnheader', { name: /Rev/ })).toBeHidden()
})

test('Graph objective nodes toggle focus inside Workspace without changing planning state', async ({ page }) => {
  await page.goto('/?view=graph')
  const objectiveNode = page.getByRole('button', { name: 'Focus objective O-1' })
  await expect(objectiveNode).toBeVisible()
  await objectiveNode.press('Enter')

  await expect(page.getByRole('heading', { name: 'Keep execution connected to intent.' })).toBeVisible()
  await expect(page).toHaveURL(/objective=O-1/)
  await objectiveNode.press('Enter')
  await expect(page).not.toHaveURL(/objective=O-1/)
})

test('Treemap objective navigation focuses the matching Objective in Workspace', async ({ page }) => {
  await page.goto('/?view=treemap')
  const navigator = page.getByRole('navigation', { name: 'Treemap objective navigation' })
  await navigator.getByRole('button', { name: 'Focus objective O-1' }).click()

  await expect(page.getByRole('heading', { name: 'Keep execution connected to intent.' })).toBeVisible()
  await expect(page).toHaveURL(/objective=O-1/)
})

test('Board and Table expose advisory dependency blockers with direct navigation', async ({ page }) => {
  const boardCard = page.locator('article[aria-label^="T-0002:"]')
  const boardBlocker = boardCard.getByRole('button', { name: /Blocked by T-0001/ })
  await expect(boardBlocker).toBeVisible()
  await expect(boardCard.getByRole('combobox', { name: 'Change T-0002 status' })).toBeEnabled()
  await boardBlocker.click()
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeVisible()

  await page.goto('/?view=table')
  const tableRow = page.locator('tbody tr').filter({
    has: page.locator('td:first-child strong').filter({ hasText: /^T-0002$/ }),
  })
  const tableBlocker = tableRow.getByRole('button', { name: /Blocked by T-0001/ })
  await expect(tableBlocker).toBeVisible()
  await expect(tableRow.getByRole('combobox', { name: 'Status for T-0002' })).toBeEnabled()
  await tableBlocker.click()
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeVisible()
})

test('Workspace readiness filter deep-links blocked and ready active work', async ({ page }) => {
  await page.getByRole('button', { name: 'Filter tasks' }).click()
  await page.getByRole('combobox', { name: 'Filter by readiness' }).selectOption('blocked')
  await expect(page).toHaveURL(/readiness=blocked/)
  await expect(page.getByText(/\d+ of \d+ tasks shown · \d+ canonical relationships/)).toBeVisible()
  await expect(page.locator('article[aria-label^="T-0002:"]')).toBeVisible()
  await expect(page.locator('article[aria-label^="T-0001:"]')).toBeHidden()

  await page.getByRole('combobox', { name: 'Filter by readiness' }).selectOption('ready')
  await expect(page).toHaveURL(/readiness=ready/)
  await expect(page.locator('article[aria-label^="T-0001:"]')).toBeVisible()
  await expect(page.locator('article[aria-label^="T-0002:"]')).toBeHidden()

  await page.getByRole('button', { name: 'Filter tasks' }).click()
  await page.getByRole('button', { name: 'Clear readiness filter Ready to act' }).click()
  await expect(page).not.toHaveURL(/readiness=/)
  await expect(page.locator('article[aria-label^="T-0001:"]')).toBeVisible()
  await expect(page.locator('article[aria-label^="T-0002:"]')).toBeVisible()
})

test('Workspace due timing deep-links active work without a due date', async ({ page }) => {
  await page.getByRole('button', { name: 'New task' }).click()
  await page.getByRole('textbox', { name: 'Task title' }).fill('Unscheduled timing smoke')
  await page.getByRole('button', { name: 'Create task' }).click()
  const drawer = page.getByRole('complementary', { name: /Task T-/ })
  await drawer.getByRole('button', { name: 'Close task drawer' }).click()

  await page.getByRole('tab', { name: 'Table' }).click()
  await page.getByRole('button', { name: 'Filter tasks' }).click()
  await page.getByRole('combobox', { name: 'Filter by due timing' }).selectOption('unscheduled')
  await expect(page).toHaveURL(/timing=unscheduled/)

  const rows = page.locator('tbody tr')
  const unscheduled = page.locator('.wsv-due--none')
  await expect.poll(async () => rows.count()).toBeGreaterThan(0)
  expect(await rows.count()).toBe(await unscheduled.count())
  await expect(page.getByRole('tabpanel', { name: 'table workspace view' }).getByText('Unscheduled timing smoke')).toBeVisible()

  await page.getByRole('button', { name: 'Filter tasks' }).click()
  await page.getByRole('button', { name: 'Clear due timing filter No due date' }).click()
  await expect(page).not.toHaveURL(/timing=/)
})

test('Focus blocks unfinished dependencies and can undo an actionable transition', async ({ page }) => {
  await page.goto('/?surface=focus')
  const blockedRow = page.getByRole('article').filter({ hasText: 'T-0002' })
  await expect(blockedRow.getByText('Blocked by T-0001')).toBeVisible()
  await expect(blockedRow.getByRole('button', { name: 'Blocked' })).toBeDisabled()
  await blockedRow.getByRole('button', { name: 'Open blocker T-0001' }).click()
  const blockerDrawer = page.getByRole('complementary', { name: 'Task T-0001' })
  await expect(blockerDrawer).toBeVisible()
  await blockerDrawer.getByRole('button', { name: 'Close task drawer' }).click()

  const row = page.getByRole('article').filter({ hasText: 'T-0005' })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: 'Mark done' }).click()
  await page.getByRole('button', { name: 'Undo' }).click()
  await expect(page.getByRole('article').filter({ hasText: 'T-0005' })).toBeVisible()
  await expect(page.getByText('T-0005 restored to started')).toBeVisible()
})

test('Focus records a human work session without changing planning status', async ({ page }) => {
  await page.goto('/?surface=focus')
  const start = page.getByRole('button', { name: /^Begin work session for / }).first()
  const taskId = (await start.getAttribute('aria-label'))!.replace('Begin work session for ', '')
  const row = start.locator('xpath=ancestor::article')
  const planningActionBefore = await row.locator('.focus-row__action').textContent()

  await start.click()
  await expect(page.getByText('Current work session')).toBeVisible()
  await expect(row.locator('.focus-row__action')).toHaveText(planningActionBefore ?? '')
  await page.getByRole('button', { name: 'Pause session' }).click()
  await expect(page.getByRole('button', { name: 'Resume session' })).toBeVisible()
  await page.getByRole('button', { name: 'Resume session' }).click()
  await page.getByRole('button', { name: 'Stop session' }).click()

  await expect(page.getByText('Worklog ready')).toBeVisible()
  const worklog = page.locator('.work-session-card--pending')
  await worklog.getByRole('textbox', { name: 'Done' }).fill(`Playwright completed human session for ${taskId}`)
  await worklog.getByRole('button', { name: 'Add to worklog' }).click()
  await expect(page.getByText('Worklog entry recorded')).toBeVisible()
  await expect(page.getByText('Worklog ready')).toBeHidden()

  await page.goto('/?surface=review')
  await expect(page.getByRole('heading', { name: 'Turn execution into evidence.' })).toBeVisible()
  await expect(
    page.getByText(`Playwright completed human session for ${taskId}`, { exact: true }),
  ).toBeVisible()
})

test('a committed planning change refreshes another open tab', async ({ context, page }) => {
  await page.goto('/?view=table')
  const secondPage = await context.newPage()
  await secondPage.goto('/?view=table')

  const firstStatus = page.getByRole('combobox', { name: 'Status for T-0004' })
  const secondStatus = secondPage.getByRole('combobox', { name: 'Status for T-0004' })
  await expect(firstStatus).toHaveValue('open')
  await expect(secondStatus).toHaveValue('open')

  await firstStatus.selectOption('started')
  await expect(page.getByText('T-0004 moved to started')).toBeVisible()
  await expect(secondStatus).toHaveValue('started')
  await secondPage.waitForTimeout(300)
  await secondPage.close()
})

test('Quick Add restores a local draft and the latest status transition is undoable', async ({ page }) => {
  await page.getByRole('button', { name: 'New task' }).click()
  await page.getByRole('textbox', { name: 'Task title' }).fill('Recover this local planning draft')
  await page.reload()
  await page.getByRole('button', { name: 'New task' }).click()
  await expect(page.getByRole('textbox', { name: 'Task title' })).toHaveValue('Recover this local planning draft')
  await page.getByRole('button', { name: 'Clear draft' }).click()
  await page.getByRole('button', { name: 'Close', exact: true }).click()

  await page.goto('/?view=table')
  await page.getByRole('button', { name: 'Filter tasks' }).click()
  await page.getByRole('combobox', { name: 'Filter by priority' }).selectOption('P0')
  await page.getByRole('button', { name: 'Filter tasks' }).click()
  await page.getByRole('button', { name: 'Save view' }).click()
  await expect(page.getByRole('textbox', { name: 'Saved view name' })).toHaveValue('P0 · Table')
  await page.getByRole('button', { name: 'Create saved view' }).click()
  await page.getByRole('button', { name: 'Clear filters' }).click()
  await page.getByRole('combobox', { name: 'Saved filters' }).selectOption({ label: 'P0 · Table' })
  await page.getByRole('button', { name: 'Filter tasks' }).click()
  await expect(page.getByRole('combobox', { name: 'Filter by priority' })).toHaveValue('P0')
  await page.getByRole('button', { name: 'Filter tasks' }).click()
  await page.getByRole('button', { name: 'Saved view actions' }).click()
  await page.getByRole('button', { name: 'Remove saved view' }).click()
  await page.getByRole('button', { name: 'Clear filters' }).click()

  const status = page.getByRole('combobox', { name: 'Status for T-0006' })
  await expect(status).toHaveValue('open')
  await status.selectOption('started')
  await page.getByRole('button', { name: 'Undo' }).click()
  await expect(status).toHaveValue('open')
  await expect(page.getByText('T-0006 restored to open')).toBeVisible()
})

test('theme switching recolors the Workspace Graph and rendered MiniMap', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/?view=graph')
  const graph = page.locator('.wsv-graph')
  const graphNode = page.locator('.wsv-graph-node').first()
  const miniMap = page.locator('.react-flow__minimap')
  await expect(graph).toBeVisible()
  await expect(graphNode).toBeVisible()
  await expect(miniMap).toBeVisible()

  await useTheme(page, 'dark')
  const dark = {
    graph: await renderedThemeStyle(graph),
    miniMap: await renderedThemeStyle(miniMap),
    node: await renderedThemeStyle(graphNode),
  }

  await useTheme(page, 'light')
  const light = {
    graph: await waitForRenderedThemeChange(graph, dark.graph, ['backgroundImage']),
    miniMap: await waitForRenderedThemeChange(miniMap, dark.miniMap, ['backgroundColor']),
    node: await waitForRenderedThemeChange(graphNode, dark.node, ['backgroundImage', 'borderColor']),
  }
  expect(light.graph.backgroundImage).not.toBe(dark.graph.backgroundImage)
  expect(light.miniMap.backgroundColor).not.toBe(dark.miniMap.backgroundColor)
  expect(light.node.backgroundImage).not.toBe(dark.node.backgroundImage)
  expect(light.node.borderColor).not.toBe(dark.node.borderColor)

  await useTheme(page, 'dark')
  const restoredGraph = await waitForRenderedThemeChange(graph, light.graph, ['backgroundImage'])
  const restoredMiniMap = await waitForRenderedThemeChange(miniMap, light.miniMap, ['backgroundColor'])
  expect(restoredGraph).toEqual(dark.graph)
  expect(restoredMiniMap).toEqual(dark.miniMap)
})

test('theme switching recolors Focus, Context Inbox, and an open dialog', async ({ page }) => {
  await page.goto('/?surface=focus')
  const focusRow = page.locator('.focus-row').first()
  await expect(focusRow).toBeVisible()
  await useTheme(page, 'dark')
  const darkFocus = await renderedThemeStyle(focusRow)
  await useTheme(page, 'light')
  const lightFocus = await waitForRenderedThemeChange(focusRow, darkFocus, ['backgroundImage', 'borderColor'])
  expect(lightFocus.backgroundImage).not.toBe(darkFocus.backgroundImage)
  expect(lightFocus.borderColor).not.toBe(darkFocus.borderColor)

  await page.goto('/?surface=inbox')
  const sourceDock = page.locator('.source-provider-dock')
  await expect(sourceDock).toBeVisible()
  await useTheme(page, 'light')
  const lightDock = await renderedThemeStyle(sourceDock)
  await page.getByRole('button', { name: 'Import packet' }).first().click()
  const dialog = page.getByRole('dialog', { name: 'Import context' })
  const dialogSurface = dialog.locator('.dialog__surface')
  await expect(dialogSurface).toBeVisible()
  const lightDialog = await renderedThemeStyle(dialogSurface)

  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await useTheme(page, 'dark')
  const darkDock = await waitForRenderedThemeChange(sourceDock, lightDock, ['backgroundImage', 'borderColor'])
  await page.getByRole('button', { name: 'Import packet' }).first().click()
  await expect(dialogSurface).toBeVisible()
  const darkDialog = await waitForRenderedThemeChange(dialogSurface, lightDialog, ['backgroundImage', 'borderColor'])
  expect(darkDock.backgroundImage).not.toBe(lightDock.backgroundImage)
  expect(darkDock.borderColor).not.toBe(lightDock.borderColor)
  expect(darkDialog.backgroundImage).not.toBe(lightDialog.backgroundImage)
  expect(darkDialog.borderColor).not.toBe(lightDialog.borderColor)
})

test('both product themes retain accessible semantic token contrast', async ({ page }) => {
  const pairs = [
    ['--ws-text-primary', '--ws-surface-raised'],
    ['--ws-graph-edge-label-text', '--ws-graph-edge-label-bg'],
    ['--ws-status-success-text', '--ws-status-success-surface'],
    ['--ws-status-warning-text', '--ws-status-warning-surface'],
    ['--ws-status-danger-text', '--ws-status-danger-surface'],
    ['--ws-text-muted', '--ws-status-neutral-surface'],
  ] as const

  for (const theme of ['dark', 'light'] as const) {
    await useTheme(page, theme)
    for (const [foreground, background] of pairs) {
      const ratio = await semanticTokenContrast(page, foreground, background)
      expect(ratio, `${theme}: ${foreground} on ${background}`).toBeGreaterThanOrEqual(4.5)
    }
  }
})

test('forced-colors mode keeps primary Board actions visible and focusable', async ({ page }) => {
  await page.emulateMedia({ forcedColors: 'active' })
  await page.goto('/?view=board')

  const newTask = page.getByRole('button', { name: 'New task' })
  const card = page.locator('article[aria-label^="T-0001:"]')
  await expect(newTask).toBeVisible()
  await expect(card).toBeVisible()
  await newTask.focus()
  await expect(newTask).toBeFocused()
  await card.focus()
  await expect(card).toBeFocused()
})

test('200 percent reflow-equivalent viewport keeps core planning actions operable', async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 480 })
  await page.goto('/?view=board')

  const newTask = page.getByRole('button', { name: 'New task' })
  await expect(page.getByRole('heading', { name: 'Keep execution connected to intent.' })).toBeVisible()
  await expect(newTask).toBeVisible()
  await expect(page.getByRole('button', { name: 'More workspace actions' })).toBeVisible()
  await newTask.click()
  await expect(page.getByRole('dialog', { name: 'New task' })).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Task title' })).toBeVisible()
})

const axeSurfaceCases = [
  { name: 'Graph', path: '/?view=graph' },
  { name: 'Board', path: '/?view=board' },
  { name: 'Treemap', path: '/?view=treemap' },
  { name: 'Table', path: '/?view=table' },
  { name: 'Focus', path: '/?surface=focus' },
  { name: 'Inbox', path: '/?surface=inbox' },
  { name: 'Daily Review', path: '/?surface=review' },
  { name: 'Objective Hub', path: '/?surface=objectives&objective=O-1' },
]

for (const { name, path } of axeSurfaceCases) {
  test(`${name} has no serious or critical axe violations`, async ({ page }) => {
    await page.goto(path)
    await expect(page.locator('main')).toBeVisible()
    const result = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze()
    const blockers = result.violations.filter(({ impact }) => (
      impact === 'serious' || impact === 'critical'
    ))
    expect(blockers, `${path}: ${blockers.map(({ id, help }) => `${id}: ${help}`).join('; ')}`).toEqual([])
  })
}
