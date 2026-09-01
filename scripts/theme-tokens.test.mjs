import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  DEFAULT_SOURCE,
  generateThemeTokens,
  renderCss,
  renderPython,
  tokenKeyToCssName,
  validateThemeTokens,
} from './generate-theme-tokens.mjs'

async function canonicalDocument() {
  return JSON.parse(await readFile(DEFAULT_SOURCE, 'utf8'))
}

test('canonical themes expose the same complete token set', async () => {
  const document = await canonicalDocument()
  assert.deepEqual(validateThemeTokens(document), [])
  assert.deepEqual(Object.keys(document.dark), Object.keys(document.light))
})

test('native surfaces and semantic status triples are present', async () => {
  const document = await canonicalDocument()
  const keys = new Set(Object.keys(document.dark))
  for (const key of [
    'native.caption',
    'native.text',
    'native.border',
    'native.overlay',
    'native.toolbar',
    'native.externalLoading',
    'brand.accent',
    'brand.ink',
  ]) {
    assert.ok(keys.has(key), `missing ${key}`)
  }
  for (const status of ['success', 'info', 'warning', 'danger', 'neutral']) {
    for (const role of ['surface', 'border', 'text']) assert.ok(keys.has(`status.${status}.${role}`))
  }
})

test('validation rejects theme drift and malformed values', async () => {
  const document = await canonicalDocument()
  delete document.light['text.primary']
  document.dark['brand.accent'] = '#12345'
  const errors = validateThemeTokens(document)
  assert.ok(errors.some((error) => error.includes('light is missing token: text.primary')))
  assert.ok(errors.some((error) => error.includes('dark.brand.accent has an invalid value')))
})

test('renderers expose stable CSS and Python APIs', async () => {
  const document = await canonicalDocument()
  assert.equal(tokenKeyToCssName('native.externalLoading'), '--ws-native-external-loading')
  assert.match(renderCss(document), /--ws-status-danger-text: #ff9aad;/)
  assert.match(renderPython(document), /THEME_TOKENS: dict\[str, dict\[str, str\]\] =/)
})

test('committed generated files are current', async () => {
  await assert.doesNotReject(generateThemeTokens({ check: true }))
})
