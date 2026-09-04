import { expect, test, type Page, type Request } from '@playwright/test'

/**
 * First acceptance slice: exactly the two nonmutating cases root selected.
 *
 * BA-T4-1 history and BA-T29-1 completed visibility. Every assertion is
 * unconditional: an absent control fails the case instead of skipping it. No
 * request other than GET is permitted for the whole run of either case.
 *
 * Selectors follow the existing e2e/workstack.spec.ts conventions (sidebar
 * disclosure 'Tasks <count>', searchbox 'Filter sidebar tasks', filter 'Filter by status').
 */

const MUTATING = new Set(['POST', 'PATCH', 'PUT', 'DELETE'])

/** One recorder per page; a mutation is a failure, never a tolerated extra. */
function recordMutations(page: Page): string[] {
  const seen: string[] = []
  page.on('request', (request: Request) => {
    if (MUTATING.has(request.method())) seen.push(`${request.method()} ${request.url()}`)
  })
  return seen
}

function param(page: Page, name: string): string | null {
  return new URL(page.url()).searchParams.get(name)
}

function taskCoordinate(page: Page): string | null {
  return param(page, 'task')
}

test('BA-T4-1 history restores the Task coordinate and drawer identity', async ({ page }) => {
  const mutations = recordMutations(page)
  await page.goto('/?view=table&doneVisibility=show')
  await expect(page.getByRole('heading', { name: 'Keep execution connected to intent.' })).toBeVisible()

  // Open T-0001 from its exact Table row.
  const firstRow = page.getByRole('row').filter({ hasText: 'T-0001' }).first()
  await expect(firstRow).toBeVisible()
  await firstRow.click()
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeVisible()
  await expect.poll(() => taskCoordinate(page)).toBe('T-0001')

  // Open T-0021 through the actual sidebar Task search.
  await page.getByRole('button', { name: /^Tasks \d+$/ }).click()
  const search = page.getByRole('searchbox', { name: 'Filter sidebar tasks' })
  await search.fill('metric ownership')
  await page.getByRole('button', { name: 'Open task T-0021: Define metric ownership' }).click()
  await expect(page.getByRole('complementary', { name: 'Task T-0021' })).toBeVisible()
  await expect.poll(() => taskCoordinate(page)).toBe('T-0021')

  await page.goBack()
  await expect.poll(() => taskCoordinate(page)).toBe('T-0001')
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeVisible()

  await page.goForward()
  await expect.poll(() => taskCoordinate(page)).toBe('T-0021')
  await expect(page.getByRole('complementary', { name: 'Task T-0021' })).toBeVisible()

  // Healthy control: clearing the selection and switching view leaves no
  // Task coordinate and no drawer behind.
  await page.getByRole('complementary', { name: 'Task T-0021' })
    .getByRole('button', { name: 'Close task drawer' }).click()
  await expect.poll(() => taskCoordinate(page)).toBeNull()
  await page.goto('/?view=board&doneVisibility=show')
  await expect.poll(() => taskCoordinate(page)).toBeNull()
  await expect(page.getByRole('complementary', { name: /^Task / })).toHaveCount(0)

  expect(mutations, 'the history case must issue no mutation').toEqual([])
})

test('BA-T29-1 completed visibility changes only membership and its own coordinate', async ({ page }) => {
  const mutations = recordMutations(page)
  await page.goto('/?view=board')
  await expect(page.getByRole('heading', { name: 'Keep execution connected to intent.' })).toBeVisible()

  // The visibility and status filters live inside the collapsible Filters panel.
  await page.getByRole('button', { name: 'Filter tasks' }).click()
  const visibility = page.getByLabel('Completed task visibility')
  const doneWitness = page.locator('article[aria-label^="T-0003:"]')
  const activeWitness = page.locator('article[aria-label^="T-0001:"]')

  // Explicit show: the done witness joins the rendered set.
  await visibility.selectOption('show')
  await expect.poll(() => param(page, 'doneVisibility')).toBe('show')
  await expect(doneWitness).toBeVisible()
  await expect(activeWitness).toBeVisible()

  // Explicit hide: only the done witness leaves.
  await visibility.selectOption('hide')
  await expect.poll(() => param(page, 'doneVisibility')).toBe('hide')
  await expect(doneWitness).toHaveCount(0)
  await expect(activeWitness).toBeVisible()

  // Default is the omitted coordinate, not a literal value in the URL.
  await visibility.selectOption('default')
  await expect.poll(() => param(page, 'doneVisibility')).toBeNull()
  await expect(activeWitness).toBeVisible()

  // status=done then hide: the source moves status back to all atomically, and
  // an omitted status=all is the correct serialization, not a literal.
  await page.getByLabel('Filter by status').selectOption('done')
  await expect(doneWitness).toBeVisible()
  await visibility.selectOption('hide')
  await expect.poll(() => param(page, 'doneVisibility')).toBe('hide')
  await expect.poll(() => param(page, 'status')).toBeNull()
  await expect(doneWitness).toHaveCount(0)

  // Only membership and the visibility coordinate moved: the view is still the
  // Board and no Task coordinate or drawer appeared as a side effect.
  expect(param(page, 'view')).toBe('board')
  expect(taskCoordinate(page)).toBeNull()
  await expect(page.getByRole('complementary', { name: /^Task / })).toHaveCount(0)

  expect(mutations, 'the visibility case must issue no mutation').toEqual([])
})
