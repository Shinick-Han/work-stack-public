import { expect, test } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

// This suite uses the configured disposable owner. It never writes personal SSOT.
test('Task -> Record progress -> saved next step returns to the same Task', async ({ page }) => {
  const next = `Resume acceptance ${Date.now()}`
  await page.goto('/?view=board&task=T-0001')
  const drawer = page.getByRole('complementary', { name: 'Task T-0001' })
  await expect(drawer.getByRole('tab', { name: 'Resume', exact: true })).toHaveAttribute('aria-selected', 'true')
  await drawer.getByRole('button', { name: 'Record progress', exact: true }).click()
  await expect(page).toHaveURL(/reviewTask=T-0001/)
  await expect(page.getByRole('combobox', { name: 'Task', exact: true })).toHaveValue('T-0001')
  await page.getByRole('textbox', { name: 'What changed?', exact: true }).fill('Verified the same Task across the resume flow.')
  await page.getByRole('textbox', { name: 'What comes next?', exact: true }).fill(next)
  await page.getByRole('button', { name: 'Save progress', exact: true }).click()
  await expect(page.getByRole('textbox', { name: 'What comes next?', exact: true })).toHaveValue('')
  await page.getByRole('button', { name: 'Open T-0001 planning detail' }).click()
  await expect(drawer.getByText(next, { exact: true })).toBeVisible()
})

test('independent Review requires explicit selection and preserves a draft when navigating away', async ({ page }) => {
  await page.goto('/?surface=review')
  const target = page.getByRole('combobox', { name: 'Task', exact: true })
  await expect(target).toHaveValue('')
  await expect(page.getByRole('button', { name: 'Save progress', exact: true })).toBeDisabled()
  await target.selectOption('T-0002')
  const draft = page.getByRole('textbox', { name: 'What comes next?', exact: true })
  await draft.fill('Do not attach this draft to another Task.')
  await page.getByRole('button', { name: /^Workspace/ }).first().click()
  await expect(page).toHaveURL(/surface=review/)
  await expect(draft).toHaveValue('Do not attach this draft to another Task.')
  await expect(target).toHaveValue('T-0002')
})

test('Review controls remain contained on narrow desktop windows', async ({ page }, testInfo) => {
  test.setTimeout(60_000)
  for (const theme of ['dark', 'light']) {
    for (const width of [1440, 1250, 960, 820, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await page.goto('/?surface=review&reviewTask=T-0001')
      const target = page.getByRole('combobox', { name: 'Task', exact: true })
      await expect(target).toHaveValue('T-0001')
      if (await page.locator('html').getAttribute('data-theme') !== theme) {
        await page.getByRole('button', { name: `Use ${theme} theme` }).click()
      }
      const overflows = await page.locator('.review-page input, .review-page select, .review-page textarea').evaluateAll((controls) =>
        controls.filter((control) => {
          const box = control.getBoundingClientRect()
          if (!box.width || !box.height) return false
          const parent = control.parentElement!.getBoundingClientRect()
          return box.left < parent.left - 1 || box.right > parent.right + 1
        }).map((control) => control.getAttribute('aria-label') ?? control.tagName))
      expect(overflows, `Contained Review controls at ${width}px in ${theme}`).toEqual([])
      if (width === 1440) {
        const accessibility = await new AxeBuilder({ page }).include('.review-page')
          .withTags(['wcag2a', 'wcag2aa']).analyze()
        expect(accessibility.violations, `Review accessibility in ${theme}`).toEqual([])
      }
      if (width === 1440 || width === 390) {
        await page.screenshot({ path: testInfo.outputPath(`review-${width}-${theme}.png`) })
      }
    }
  }
})
