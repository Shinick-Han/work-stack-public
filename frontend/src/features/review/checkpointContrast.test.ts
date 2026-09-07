import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, test } from 'vitest'

/**
 * Contrast regression for the Checkpoint history stylesheet.
 *
 * Every line the card renders — "None recorded", the Additional recorded data
 * labels, the checkpoint id, ordinal and revision — is a recorded audit fact,
 * not decoration. They are meant to read as SECONDARY, and this file pins the
 * rule that secondary is expressed with size, weight, rule and position rather
 * than by dimming informative text below what a reader can resolve.
 *
 * The check reads the real stylesheet and the real generated tokens, so it also
 * fails if a future palette change darkens a token these rules depend on.
 */

/** Read from the Vitest root (frontend/), so this reads the SHIPPED files. */
function readSource(relative: string): string {
  return readFileSync(resolve(process.cwd(), relative), 'utf-8')
}

const STYLESHEET = readSource('src/styles.css')
const TOKENS = readSource('src/generated/theme-tokens.css')

/** WCAG 2.1 AA for normal-size text. These rules run at 9-11px. */
const AA_NORMAL_TEXT = 4.5

/** The surfaces a checkpoint row actually paints on. */
const SURFACE_TOKENS = ['--ws-control-bg', '--ws-surface-base'] as const

/** The token the reviewer found on informative text; it is 3:1 at best. */
const DISABLED_TOKEN = '--ws-text-disabled'

interface CssRule {
  selector: string
  declarations: string
}

function parseRules(css: string): CssRule[] {
  const rules: CssRule[] = []
  // The stylesheet is flat, single-level CSS; at-rule bodies are skipped below.
  for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = match[1].replace(/\/\*[\s\S]*?\*\//g, '').trim()
    if (selector === '' || selector.startsWith('@')) continue
    rules.push({ selector, declarations: match[2] })
  }
  return rules
}

function themeTokens(theme: 'dark' | 'light'): Map<string, string> {
  const marker = theme === 'dark' ? ":root[data-theme='dark']" : ":root[data-theme='light']"
  const start = TOKENS.indexOf(marker)
  expect(start, `${marker} block is missing from the generated tokens`).toBeGreaterThan(-1)
  const body = TOKENS.slice(TOKENS.indexOf('{', start) + 1, TOKENS.indexOf('}', start))
  const values = new Map<string, string>()
  for (const declaration of body.matchAll(/(--[\w-]+):\s*([^;]+);/g)) {
    values.set(declaration[1], declaration[2].trim())
  }
  return values
}

function channelLuminance(channel: number): number {
  return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
}

function relativeLuminance(hex: string): number {
  const value = hex.trim().replace('#', '')
  expect(value, `expected a six-digit hex colour, got "${hex}"`).toMatch(/^[0-9a-fA-F]{6}$/)
  const [red, green, blue] = [0, 2, 4].map((offset) => (
    channelLuminance(parseInt(value.slice(offset, offset + 2), 16) / 255)
  ))
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

function contrastRatio(foreground: string, background: string): number {
  const [high, low] = [relativeLuminance(foreground), relativeLuminance(background)]
    .sort((left, right) => right - left)
  return (high + 0.05) / (low + 0.05)
}

const CHECKPOINT_RULES = parseRules(STYLESHEET)
  .filter((rule) => rule.selector.includes('checkpoint'))

/** Only `var(--token)` colours are governed; nothing here uses a literal. */
function colourToken(rule: CssRule): string | null {
  const declared = /(?:^|;)\s*color:\s*([^;]+)/.exec(rule.declarations)
  if (declared === null) return null
  const token = /^var\((--[\w-]+)\)$/.exec(declared[1].trim())
  return token === null ? null : token[1]
}

describe('checkpoint history stylesheet contrast', () => {
  test('the stylesheet was actually found and parsed', () => {
    expect(CHECKPOINT_RULES.length).toBeGreaterThan(20)
  })

  test('no checkpoint rule reaches for the disabled token', () => {
    // The exact regression: informative audit facts were painted with the
    // token reserved for controls a reader cannot act on.
    const offenders = CHECKPOINT_RULES
      .filter((rule) => rule.declarations.includes(DISABLED_TOKEN))
      .map((rule) => rule.selector)
    expect(offenders).toEqual([])
  })

  test.each([
    ['.checkpoint-facts__none', 'None recorded'],
    ['.checkpoint-entry__extras dt', 'Additional recorded data labels'],
    ['.checkpoint-entry__identifiers', 'checkpoint id, ordinal and revision'],
  ])('%s (%s) declares a colour of its own', (selector) => {
    const rule = CHECKPOINT_RULES.find((candidate) => candidate.selector === selector)
    expect(rule, `${selector} is missing from the stylesheet`).toBeDefined()
    expect(colourToken(rule as CssRule)).not.toBeNull()
  })

  test.each(['dark', 'light'] as const)(
    'every checkpoint text colour clears AA on every surface in the %s theme',
    (theme) => {
      const tokens = themeTokens(theme)
      const failures: string[] = []
      for (const rule of CHECKPOINT_RULES) {
        // A list marker duplicates the Done/Next/Blockers label beside it, so
        // it is decoration rather than the sole carrier of meaning.
        if (rule.selector.includes('::marker')) continue
        const token = colourToken(rule)
        if (token === null) continue
        const foreground = tokens.get(token)
        expect(foreground, `${token} is not defined in the ${theme} theme`).toBeDefined()
        for (const surface of SURFACE_TOKENS) {
          const background = tokens.get(surface) as string
          const ratio = contrastRatio(foreground as string, background)
          if (ratio < AA_NORMAL_TEXT) {
            failures.push(
              `${rule.selector} uses ${token} on ${surface}: ${ratio.toFixed(2)}:1`,
            )
          }
        }
      }
      expect(failures).toEqual([])
    },
  )

  test('secondary hierarchy survives without dimming the text', () => {
    const declarationsFor = (selector: string) => {
      const rule = CHECKPOINT_RULES.find((candidate) => candidate.selector === selector)
      expect(rule, `${selector} is missing from the stylesheet`).toBeDefined()
      return (rule as CssRule).declarations
    }
    // The Task heading still leads on size and weight...
    expect(declarationsFor('.checkpoint-entry__task strong')).toContain('font-size: 11px')
    expect(declarationsFor('.checkpoint-entry__task strong')).toContain('font-weight: 650')
    // ...while provenance stays smaller and set apart by its own rule.
    const identifiers = declarationsFor('.checkpoint-entry__identifiers')
    expect(identifiers).toContain('font-size: 9px')
    expect(identifiers).toContain('border-top:')
    // An absent list is marked as absent by style, not by being hard to read.
    expect(declarationsFor('.checkpoint-facts__none')).toContain('font-style: italic')
  })
})
