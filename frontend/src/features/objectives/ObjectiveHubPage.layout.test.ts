// @vitest-environment node
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium, type Browser } from 'playwright'
import { afterAll, beforeAll, expect, test } from 'vitest'

const srcRoot = join(dirname(fileURLToPath(import.meta.url)), '../..')
const themeTokens = readFileSync(join(srcRoot, 'generated/theme-tokens.css'), 'utf8')
const productStyles = readFileSync(join(srcRoot, 'styles.css'), 'utf8')

const NARROW_DESKTOP_WIDTHS = [1250, 1100] as const
const MIN_KR_DESCRIPTION = 140
const MIN_KR_TARGET = 120
const MIN_KR_RANGE = 80
const MIN_KR_BUTTON = 44

type Box = { bottom: number; height: number; left: number; right: number; top: number; width: number }

function hubMarkup() {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>${themeTokens}\n${productStyles}</style>
</head>
<body>
  <div class="app-shell">
    <aside class="app-sidebar"></aside>
    <div class="app-stage">
      <header class="app-topbar"></header>
      <main class="app-main">
        <div class="app-main__write-surface">
          <section class="objective-hub">
            <div class="objective-hub-layout">
              <aside class="objective-index">
                <button class="is-active" type="button">
                  <span><strong>O-1</strong></span>
                  <b>Make the goal–work chain explicit for every planning surface.</b>
                  <small>2026-Q3 · 4 active of 4 aligned · 40%</small>
                </button>
              </aside>
              <div class="objective-detail-stage">
                <section class="objective-overview">
                  <div class="objective-overview__identity">
                    <span>O-1 · 2026-Q3</span>
                    <h2>Make the goal–work chain explicit for every planning surface.</h2>
                    <small>Revision 0 · updated unknown</small>
                  </div>
                  <div class="objective-editor">
                    <label>
                      <span>Objective title</span>
                      <input value="Make the goal–work chain explicit for every planning surface.">
                    </label>
                    <label>
                      <span>Objective quarter</span>
                      <input data-testid="objective-quarter" value="2026-Q3">
                    </label>
                    <button class="button button--secondary" type="button">Save Objective</button>
                  </div>
                  <label>
                    <span>Objective status</span>
                    <select><option>active</option></select>
                  </label>
                </section>
                <section class="kr-panel">
                  <article class="kr-row">
                    <label><span>Key Result description</span><input value="Review five real work days against the same Objective"></label>
                    <label><span>Key Result target</span><input value="5 days"></label>
                    <label><span>Progress</span><div class="kr-progress-input"><input max="100" min="0" type="range" value="40"><output>40%</output></div></label>
                    <label><span>Status</span><select><option>active</option></select></label>
                    <button class="button button--secondary" type="button">Save KR</button>
                  </article>
                </section>
              </div>
            </div>
          </section>
        </div>
      </main>
    </div>
  </div>
</body>
</html>`
}

let browser: Browser

beforeAll(async () => {
  browser = await chromium.launch({ headless: true })
}, 30_000)

afterAll(async () => {
  await browser.close()
})

async function measureObjectiveEditor(width: number) {
  const page = await browser.newPage({ viewport: { height: 900, width } })
  await page.setContent(hubMarkup(), { waitUntil: 'load' })
  const layout = await page.evaluate(() => {
    const rect = (selector: string) => {
      const element = document.querySelector<HTMLElement>(selector)
      if (!element) throw new Error(`Missing ${selector}`)
      const bounds = element.getBoundingClientRect()
      return {
        bottom: bounds.bottom,
        height: bounds.height,
        left: bounds.left,
        right: bounds.right,
        top: bounds.top,
        width: bounds.width,
      }
    }
    const main = document.querySelector<HTMLElement>('.app-main')
    if (!main) throw new Error('Missing .app-main')
    return {
      hub: rect('.objective-hub'),
      krButton: rect('.kr-row > .button'),
      krDescription: rect('.kr-row > label:nth-child(1) input'),
      krRange: rect('.kr-row input[type="range"]'),
      krTarget: rect('.kr-row > label:nth-child(2) input'),
      mainClientWidth: main.clientWidth,
      mainScrollWidth: main.scrollWidth,
      overview: rect('.objective-overview'),
      quarter: rect('[data-testid="objective-quarter"]'),
      save: rect('.objective-editor > .button'),
      stage: rect('.objective-detail-stage'),
      viewportWidth: window.innerWidth,
    }
  })
  await page.close()
  return layout
}

function expectHorizontallyInside(inner: Box, outerRight: number, outerLeft = 0, slack = 1) {
  expect(inner.left).toBeGreaterThanOrEqual(outerLeft - slack)
  expect(inner.right).toBeLessThanOrEqual(outerRight + slack)
}

function expectInside(inner: Box, outer: Box, slack = 1) {
  expectHorizontallyInside(inner, outer.right, outer.left, slack)
  expect(inner.top).toBeGreaterThanOrEqual(outer.top - slack)
  expect(inner.bottom).toBeLessThanOrEqual(outer.bottom + slack)
}

function expectUsableKrControls(layout: {
  krButton: Box
  krDescription: Box
  krRange: Box
  krTarget: Box
}) {
  expect(layout.krDescription.width).toBeGreaterThanOrEqual(MIN_KR_DESCRIPTION)
  expect(layout.krTarget.width).toBeGreaterThanOrEqual(MIN_KR_TARGET)
  expect(layout.krRange.width).toBeGreaterThanOrEqual(MIN_KR_RANGE)
  expect(layout.krButton.width).toBeGreaterThanOrEqual(MIN_KR_BUTTON)
  expect(layout.krButton.height).toBeGreaterThanOrEqual(32)
}

for (const width of NARROW_DESKTOP_WIDTHS) {
  test(`Objective quarter, Save, and KR controls stay usable at ${width}px`, async () => {
    const layout = await measureObjectiveEditor(width)
    expect(layout.viewportWidth).toBe(width)
    expectInside(layout.quarter, layout.overview)
    expectInside(layout.save, layout.overview)
    expectInside(layout.quarter, layout.hub)
    expectInside(layout.save, layout.hub)
    expectHorizontallyInside(layout.quarter, layout.viewportWidth)
    expectHorizontallyInside(layout.save, layout.viewportWidth)
    expect(layout.stage.right).toBeLessThanOrEqual(layout.viewportWidth + 1)
    expect(layout.mainScrollWidth).toBeLessThanOrEqual(layout.mainClientWidth + 1)
    expectUsableKrControls(layout)
  }, 30_000)
}
