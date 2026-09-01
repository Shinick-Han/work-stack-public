#!/usr/bin/env node

import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const SCRIPT_PATH = fileURLToPath(import.meta.url)
const REPO_ROOT = path.resolve(path.dirname(SCRIPT_PATH), '..')

export const DEFAULT_SOURCE = path.join(REPO_ROOT, 'theme', 'theme-tokens.json')
export const DEFAULT_CSS_OUTPUT = path.join(REPO_ROOT, 'frontend', 'src', 'generated', 'theme-tokens.css')
export const DEFAULT_PYTHON_OUTPUT = path.join(
  REPO_ROOT,
  'desktop',
  'python-webview-shell',
  'generated',
  'theme_tokens.py',
)

const THEMES = ['dark', 'light']
const TOKEN_KEY_PATTERN = /^[a-z][A-Za-z0-9]*(?:\.[a-z0-9][A-Za-z0-9]*)*$/
const COLOR_PATTERN = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/
const REQUIRED_NATIVE_KEYS = [
  'native.caption',
  'native.text',
  'native.border',
  'native.overlay',
  'native.toolbar',
  'native.externalLoading',
  'brand.accent',
  'brand.ink',
]

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function validateShadow(value) {
  const parts = value.trim().split(/\s+/)
  if (parts.length < 4 || parts.length > 5 || !COLOR_PATTERN.test(parts.at(-1))) return false
  return parts.slice(0, -1).every((part) => part === '0' || /^-?\d+(?:\.\d+)?px$/.test(part))
}

function validateTokenValue(key, value) {
  if (typeof value !== 'string' || value.trim() !== value || value.length === 0) return false
  return key.startsWith('shadow.') ? validateShadow(value) : COLOR_PATTERN.test(value)
}

function requiredTriples(prefixes, keys, errors) {
  for (const prefix of prefixes) {
    for (const suffix of ['surface', 'border', 'text']) {
      const key = `${prefix}.${suffix}`
      if (!keys.has(key)) errors.push(`missing required semantic token: ${key}`)
    }
  }
}

export function validateThemeTokens(document) {
  const errors = []
  if (!isRecord(document)) return ['theme token document must be an object']

  const themeNames = Object.keys(document).sort()
  if (themeNames.join(',') !== [...THEMES].sort().join(',')) {
    errors.push(`theme token document must contain exactly: ${THEMES.join(', ')}`)
  }

  for (const theme of THEMES) {
    if (!isRecord(document[theme])) errors.push(`${theme} must be an object`)
  }
  if (errors.length > 0) return errors

  const canonicalKeys = Object.keys(document.dark)
  const canonicalSet = new Set(canonicalKeys)
  const lightSet = new Set(Object.keys(document.light))

  for (const key of canonicalKeys) {
    if (!TOKEN_KEY_PATTERN.test(key)) errors.push(`invalid token key: ${key}`)
    if (!lightSet.has(key)) errors.push(`light is missing token: ${key}`)
  }
  for (const key of lightSet) {
    if (!canonicalSet.has(key)) errors.push(`light has unexpected token: ${key}`)
  }

  const cssNames = new Map()
  for (const key of canonicalKeys) {
    const cssName = tokenKeyToCssName(key)
    if (cssNames.has(cssName)) {
      errors.push(`token keys ${cssNames.get(cssName)} and ${key} both generate ${cssName}`)
    }
    cssNames.set(cssName, key)
  }

  for (const theme of THEMES) {
    for (const [key, value] of Object.entries(document[theme])) {
      if (!validateTokenValue(key, value)) errors.push(`${theme}.${key} has an invalid value: ${String(value)}`)
    }
  }

  for (const key of REQUIRED_NATIVE_KEYS) {
    if (!canonicalSet.has(key)) errors.push(`missing required native token: ${key}`)
  }
  requiredTriples(
    ['status.success', 'status.info', 'status.warning', 'status.danger', 'status.neutral'],
    canonicalSet,
    errors,
  )
  requiredTriples(['priority.p0', 'priority.p1', 'priority.p2', 'priority.p3'], canonicalSet, errors)

  return errors
}

export function tokenKeyToCssName(key) {
  const kebab = key
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/\./g, '-')
    .toLowerCase()
  return `--ws-${kebab}`
}

function generatedBanner(sourcePath, commentPrefix) {
  const relativeSource = path.relative(REPO_ROOT, sourcePath).replaceAll('\\', '/')
  return `${commentPrefix} Generated from ${relativeSource} by scripts/generate-theme-tokens.mjs. Do not edit.\n`
}

export function renderCss(document, sourcePath = DEFAULT_SOURCE) {
  const keys = Object.keys(document.dark)
  const block = (theme) => keys.map((key) => `  ${tokenKeyToCssName(key)}: ${document[theme][key]};`).join('\n')
  return [
    generatedBanner(sourcePath, '/*').replace(/\n$/, ' */\n'),
    ':root,',
    ":root[data-theme='dark'] {",
    block('dark'),
    '}',
    '',
    ":root[data-theme='light'] {",
    block('light'),
    '}',
    '',
  ].join('\n')
}

export function renderPython(document, sourcePath = DEFAULT_SOURCE) {
  const serialized = JSON.stringify(document, null, 2)
  return [
    generatedBanner(sourcePath, '#').trimEnd(),
    '',
    'THEME_TOKENS: dict[str, dict[str, str]] = ' + serialized,
    '',
  ].join('\n')
}

export async function loadAndValidate(sourcePath = DEFAULT_SOURCE) {
  let document
  try {
    document = JSON.parse(await readFile(sourcePath, 'utf8'))
  } catch (error) {
    throw new Error(`could not read theme tokens from ${sourcePath}: ${error.message}`)
  }
  const errors = validateThemeTokens(document)
  if (errors.length > 0) throw new Error(`invalid theme tokens:\n- ${errors.join('\n- ')}`)
  return document
}

async function writeGeneratedFile(outputPath, contents) {
  await mkdir(path.dirname(outputPath), { recursive: true })
  await writeFile(outputPath, contents, 'utf8')
}

async function checkGeneratedFile(outputPath, expected) {
  let current
  try {
    current = await readFile(outputPath, 'utf8')
  } catch {
    return `${path.relative(REPO_ROOT, outputPath)} is missing`
  }
  return current === expected ? null : `${path.relative(REPO_ROOT, outputPath)} is stale`
}

export async function generateThemeTokens({ check = false, sourcePath = DEFAULT_SOURCE } = {}) {
  const document = await loadAndValidate(sourcePath)
  const outputs = [
    [DEFAULT_CSS_OUTPUT, renderCss(document, sourcePath)],
    [DEFAULT_PYTHON_OUTPUT, renderPython(document, sourcePath)],
  ]

  if (check) {
    const failures = (await Promise.all(outputs.map(([output, contents]) => checkGeneratedFile(output, contents)))).filter(Boolean)
    if (failures.length > 0) throw new Error(`generated theme tokens are not current:\n- ${failures.join('\n- ')}`)
    return outputs.map(([output]) => output)
  }

  await Promise.all(outputs.map(([output, contents]) => writeGeneratedFile(output, contents)))
  return outputs.map(([output]) => output)
}

async function main() {
  const arguments_ = new Set(process.argv.slice(2))
  const known = new Set(['--check'])
  const unknown = [...arguments_].filter((argument) => !known.has(argument))
  if (unknown.length > 0) throw new Error(`unknown argument(s): ${unknown.join(', ')}`)
  const outputs = await generateThemeTokens({ check: arguments_.has('--check') })
  const verb = arguments_.has('--check') ? 'verified' : 'generated'
  for (const output of outputs) console.log(`${verb} ${path.relative(REPO_ROOT, output)}`)
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error) => {
    console.error(error.message)
    process.exitCode = 1
  })
}
