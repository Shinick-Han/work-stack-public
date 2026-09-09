import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from 'vitest'

import { projectCaptureBriefCatalog } from './captureBriefCatalog'

const FIXTURES = join(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../contracts/capture-brief-sources-v1/cases.json',
)

type SharedCase = {
  id: string
  task_id: string
  context: unknown
  expected_sources: unknown[]
  cli_overflow: boolean
  gui_omitted: number
}

const payload = JSON.parse(readFileSync(FIXTURES, 'utf8')) as {
  task_id: string
  cases: SharedCase[]
}

test('shared capture-brief fixtures exist as committed JSON', () => {
  expect(payload.cases.map((item) => item.id)).toEqual([
    'empty',
    'legacy-no-evidence',
    'v1-1-counts',
    'mixed-link-conversion-drop',
    'cap-overflow',
    'leak-fence-canaries',
  ])
  expect(readdirSync(dirname(FIXTURES))).toContain('cases.json')
})

test('TS catalog projection matches the shared expected source objects', () => {
  for (const item of payload.cases) {
    const catalog = projectCaptureBriefCatalog(item.context, item.task_id)
    if (item.expected_sources.length === 0) {
      expect(catalog, item.id).toEqual({ kind: 'empty' })
      expect(item.gui_omitted, item.id).toBe(0)
      continue
    }
    expect(catalog.kind, item.id).toBe('ready')
    if (catalog.kind !== 'ready') continue
    expect(catalog.sources, item.id).toEqual(item.expected_sources)
    expect(catalog.omitted, item.id).toBe(item.gui_omitted)
    const rendered = JSON.stringify(catalog.sources)
    expect(rendered, item.id).not.toContain('LEAK-BODY')
    expect(rendered, item.id).not.toContain('LEAK-REF')
    expect(rendered, item.id).not.toContain('engine-q-LEAK')
    expect(rendered, item.id).not.toContain('https://leak.example')
  }
})
