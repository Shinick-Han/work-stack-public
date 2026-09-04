import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.goto('/?view=board')
  await expect(page.getByRole('heading', { name: 'Keep execution connected to intent.' })).toBeVisible()
})

test('Board selection and deselection share the authoritative Task drawer', async ({ page }) => {
  await expect(page.getByRole('tabpanel', { name: 'board workspace view' })).toBeVisible()
  const card = page.locator('article.wsv-task-card:not(.is-overlay)[aria-label^="T-0001:"]')
  await expect(card).toBeVisible()

  await card.click()
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeVisible()
  await expect(page).toHaveURL(/task=T-0001/)

  await card.click()
  await expect(page.getByRole('complementary', { name: 'Task T-0001' })).toBeHidden()
  await expect(page).not.toHaveURL(/task=/)
})

test('Graph keyboard navigation focuses the selected objective', async ({ page }) => {
  await page.goto('/?view=graph')
  const objective = page.getByRole('button', { name: 'Focus objective O-1' })
  await expect(objective).toBeVisible()
  await objective.focus()
  await expect(objective).toBeFocused()
  await objective.press('Enter')

  await expect(page.getByRole('button', { name: 'Clear objective filter O-1' })).toBeVisible()
  await expect(page).toHaveURL(/objective=O-1/)
})
