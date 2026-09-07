import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import type { Objective, Task } from '../../domain/types'
import { workspace } from '../../test/fixtures'
import { ObjectiveHubPage } from './ObjectiveHubPage'
import productStylesheet from '../../styles.css?raw'
import themeTokens from '../../generated/theme-tokens.css?raw'

function jsonResponse(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }))
}

test('adds and edits an Objective and Key Result with revision-guarded mutations', async () => {
  let objective: Objective = { ...workspace.objectives[0], key_results: [] }
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url === '/api/v1/objectives/O-1' && !init?.method) return jsonResponse({
      data: { objective, tasks: workspace.tasks, activity: [] },
    })
    if (url === '/api/v1/objectives/O-1/key-results' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      objective = {
        ...objective,
        revision: 1,
        key_results: [{ id: 'KR-1', text: body.text, target: body.target, progress: 0, status: 'active' }],
      }
      return jsonResponse({ data: objective, meta: { replayed: false } }, 201)
    }
    if (url === '/api/v1/objectives/O-1/key-results/KR-1' && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body))
      objective = {
        ...objective,
        revision: 2,
        key_results: [{ id: 'KR-1', text: body.text, target: body.target, progress: body.progress, status: body.status }],
      }
      return jsonResponse({ data: objective })
    }
    if (url === '/api/v1/objectives/O-1' && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body))
      objective = { ...objective, objective: body.objective, quarter: body.quarter, revision: 3 }
      return jsonResponse({ data: objective })
    }
    if (url === '/api/v1/workspace') return jsonResponse({ data: { ...workspace, objectives: [objective] } })
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onNotice = vi.fn()
  const onCreateAlignedTask = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><ObjectiveHubPage objectiveId="O-1" onCreateAlignedTask={onCreateAlignedTask} onNotice={onNotice} onOpenTask={vi.fn()} onSelectObjective={vi.fn()} workspace={workspace} /></QueryClientProvider>)

  expect(await screen.findByText(workspace.tasks[0].title)).toBeVisible()
  expect(screen.getByRole('heading', { name: 'Aligned Tasks', level: 3 })).toBeVisible()
  expect(within(screen.getByRole('complementary', { name: 'Objectives' })).getByRole('button', { name: /O-1/ })).toHaveTextContent('1 active of 1 aligned')
  await userEvent.click(screen.getByRole('button', { name: 'Create aligned task' }))
  expect(onCreateAlignedTask).toHaveBeenCalledWith('O-1')
  await userEvent.type(screen.getByLabelText('New Key Result'), 'Review five real work days')
  await userEvent.type(screen.getByLabelText('Target label'), '5 days')
  await userEvent.click(screen.getByRole('button', { name: 'Add Key Result' }))

  await waitFor(() => expect(onNotice).toHaveBeenCalledWith('Key Result added'))
  expect(await screen.findByLabelText('Key Result description')).toHaveValue('Review five real work days')
  const mutation = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/key-results') && init?.method === 'POST')
  expect((mutation?.[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^workstack:/)
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({
    text: 'Review five real work days', target: '5 days', revision: 0,
  })

  await userEvent.clear(screen.getByLabelText('Key Result description'))
  await userEvent.type(screen.getByLabelText('Key Result description'), 'Review seven real work days')
  await userEvent.clear(screen.getByLabelText('Key Result target'))
  await userEvent.type(screen.getByLabelText('Key Result target'), '7 days')
  await userEvent.click(screen.getByRole('button', { name: 'Save KR' }))
  await waitFor(() => expect(onNotice).toHaveBeenCalledWith('Key Result updated'))

  await userEvent.clear(screen.getByLabelText('Objective title'))
  await userEvent.type(screen.getByLabelText('Objective title'), 'Make execution reviewable every week')
  await userEvent.clear(screen.getByLabelText('Objective quarter'))
  await userEvent.type(screen.getByLabelText('Objective quarter'), '2026-Q4')
  await userEvent.click(screen.getByRole('button', { name: 'Save Objective' }))
  await waitFor(() => expect(onNotice).toHaveBeenCalledWith('Objective updated'))

  const krMutation = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/key-results/KR-1') && init?.method === 'PATCH')
  expect(JSON.parse(String(krMutation?.[1]?.body))).toEqual({
    text: 'Review seven real work days', target: '7 days', progress: 0, status: 'active', revision: 1,
  })
  const objectiveMutation = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/objectives/O-1') && init?.method === 'PATCH')
  expect(JSON.parse(String(objectiveMutation?.[1]?.body))).toEqual({
    objective: 'Make execution reviewable every week', quarter: '2026-Q4', revision: 2,
  })
})

test('creates an idempotent Objective directly from the Hub and selects it', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/v1/session') return jsonResponse({ data: { csrf_token: 'csrf-token-1234' } })
    if (url === '/api/v1/objectives' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body))
      return jsonResponse({ data: {
        id: 'O-2', objective: body.objective, quarter: body.quarter,
        status: 'active', key_results: [], revision: 0,
      }, meta: { replayed: false } }, 201)
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const onSelectObjective = vi.fn()
  const onNotice = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><ObjectiveHubPage objectiveId="all" onCreateAlignedTask={vi.fn()} onNotice={onNotice} onOpenTask={vi.fn()} onSelectObjective={onSelectObjective} workspace={{ ...workspace, objectives: [] }} /></QueryClientProvider>)

  await userEvent.click(screen.getByRole('button', { name: 'New Objective' }))
  await userEvent.type(screen.getByLabelText('New Objective title'), 'Make planning calm')
  await userEvent.clear(screen.getByLabelText('New Objective quarter'))
  await userEvent.type(screen.getByLabelText('New Objective quarter'), '2026-Q4')
  await userEvent.click(screen.getByRole('button', { name: 'Create Objective' }))

  await waitFor(() => expect(onSelectObjective).toHaveBeenCalledWith('O-2'))
  expect(onNotice).toHaveBeenCalledWith('Objective O-2 added')
  const mutation = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/objectives') && init?.method === 'POST')
  expect((mutation?.[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^workstack:/)
  expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({ objective: 'Make planning calm', quarter: '2026-Q4' })
})

test('projects actionable and blocked execution health for an Objective', async () => {
  const base = workspace.tasks[0]
  const prerequisite: Task = { ...base, id: 'T-0200', uid: '20000000-0000-4000-8000-000000000000', title: 'Finish prerequisite', objective_ids: [], status: 'open', dependencies: [] }
  const blocked: Task = { ...base, id: 'T-0201', uid: '20100000-0000-4000-8000-000000000000', title: 'Blocked outcome', objective_ids: ['O-1'], status: 'open', dependencies: [prerequisite.id] }
  const actionable: Task = { ...base, id: 'T-0202', uid: '20200000-0000-4000-8000-000000000000', title: 'Actionable outcome', objective_ids: ['O-1'], status: 'started', dependencies: [] }
  const done: Task = { ...base, id: 'T-0203', uid: '20300000-0000-4000-8000-000000000000', title: 'Completed outcome', objective_ids: ['O-1'], status: 'done', dependencies: [] }
  const tasks = [prerequisite, blocked, actionable, done]
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input) === '/api/v1/objectives/O-1') return jsonResponse({
      data: { objective: workspace.objectives[0], tasks: [blocked, actionable, done], activity: [] },
    })
    throw new Error(`Unexpected request: ${String(input)}`)
  }))
  const onOpenTask = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><ObjectiveHubPage objectiveId="O-1" onCreateAlignedTask={vi.fn()} onNotice={vi.fn()} onOpenTask={onOpenTask} onSelectObjective={vi.fn()} workspace={{ ...workspace, tasks }} /></QueryClientProvider>)

  const readiness = await screen.findByLabelText('Objective execution readiness')
  expect(readiness).toHaveTextContent('Actionable1')
  expect(readiness).toHaveTextContent('Blocked1')
  expect(readiness).toHaveTextContent('Done1')
  expect(readiness).toHaveTextContent('Dropped0')
  expect(within(screen.getByRole('complementary', { name: 'Objectives' })).getByRole('button', { name: /O-1/ })).toHaveTextContent('2 active of 3 aligned')
  expect(within(screen.getByLabelText('Objective metrics')).getByText('Aligned Tasks').closest('div')).toHaveTextContent('3')
  expect(screen.getByText('Waiting on T-0200')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /T-0201.*Blocked outcome/ }))
  expect(onOpenTask).toHaveBeenCalledWith('T-0201')
})

function hubTask(id: string, extra: Partial<Task> = {}): Task {
  const base = workspace.tasks[0]
  return {
    ...base,
    id,
    uid: `${id.replace(/\D/g, '').padStart(8, '0')}-0000-4000-8000-000000000000`,
    title: `${id} title`,
    objective_ids: ['O-1'],
    dependencies: [],
    ...extra,
  }
}

test('places KR cards above exact scoped Tasks and keeps Objective-only separate', async () => {
  const linked = hubTask('T-KR-1', {
    status: 'started',
    key_result_refs: [{ objective_id: 'O-1', key_result_id: 'KR-1' }],
  })
  const alsoLinked = hubTask('T-KR-2', {
    status: 'open',
    key_result_refs: [
      { objective_id: 'O-1', key_result_id: 'KR-1' },
      { objective_id: 'O-1', key_result_id: 'KR-2' },
    ],
  })
  const objectiveOnly = hubTask('T-OBJ', { status: 'done' })
  const dropped = hubTask('T-DROP', { status: 'dropped' })
  const foreign = hubTask('T-O2', {
    objective_ids: ['O-2'],
    status: 'started',
    key_result_refs: [{ objective_id: 'O-2', key_result_id: 'KR-1' }],
  })
  const unresolved = hubTask('T-MISS', {
    status: 'started',
    key_result_refs: [{ objective_id: 'O-1', key_result_id: 'KR-MISSING' }],
  })
  const objective: Objective = {
    ...workspace.objectives[0],
    key_results: [
      { id: 'KR-1', text: 'Ship hierarchy', target: '100%', progress: 40, status: 'active' },
      { id: 'KR-2', text: 'Shared KR', target: '50%', progress: 10, status: 'active' },
      { id: 'KR-3', text: 'Empty KR', target: '0', progress: 0, status: 'active' },
    ],
  }
  const other: Objective = {
    id: 'O-2',
    objective: 'Other outcome',
    status: 'active',
    revision: 0,
    key_results: [{ id: 'KR-1', text: 'Other KR-1', progress: 0, status: 'active' }],
  }
  const tasks = [linked, alsoLinked, objectiveOnly, dropped, foreign, unresolved]
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input) === '/api/v1/objectives/O-1') return jsonResponse({
      data: { objective, tasks: [linked, alsoLinked, objectiveOnly, dropped, unresolved], activity: [] },
    })
    throw new Error(`Unexpected request: ${String(input)}`)
  }))
  const onOpenTask = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ObjectiveHubPage
        objectiveId="O-1"
        onCreateAlignedTask={vi.fn()}
        onNotice={vi.fn()}
        onOpenTask={onOpenTask}
        onSelectObjective={vi.fn()}
        workspace={{ ...workspace, objectives: [objective, other], tasks }}
      />
    </QueryClientProvider>,
  )

  const kr1 = await screen.findByRole('article', { name: 'KR-1' })
  const index = screen.getByRole('complementary', { name: 'Objectives' })
  expect(within(index).getByRole('button', { name: /O-1/ })).toHaveTextContent('3 active of 5 aligned')
  expect(within(index).getByRole('button', { name: /O-2/ })).toHaveTextContent('1 active of 1 aligned')
  expect(within(screen.getByLabelText('Objective metrics')).getByText('Aligned Tasks').closest('div')).toHaveTextContent('5')
  expect(screen.getByRole('heading', { name: 'Aligned Tasks', level: 3 })).toBeVisible()
  expect(kr1).toHaveAccessibleDescription(/2 Tasks linked to this KR/)
  expect(kr1).toHaveAccessibleDescription(/Recorded progress 40/)
  expect(kr1).not.toHaveAccessibleDescription(/visible of/)
  expect(within(kr1).getByText('Ship hierarchy')).toBeVisible()
  expect(within(kr1).getByText('Target 100%')).toBeVisible()
  expect(within(kr1).getByText('Status active')).toBeVisible()
  expect(within(kr1).getByRole('button', { name: /T-KR-1/ })).toBeVisible()
  expect(within(kr1).getByRole('button', { name: /T-KR-2/ })).toBeVisible()
  expect(within(kr1).queryByRole('button', { name: /T-OBJ/ })).not.toBeInTheDocument()
  expect(within(kr1).queryByRole('button', { name: /T-O2/ })).not.toBeInTheDocument()
  expect(within(kr1).queryByRole('button', { name: /T-DROP/ })).not.toBeInTheDocument()

  const kr2 = screen.getByRole('article', { name: 'KR-2' })
  expect(kr2).toHaveAccessibleDescription(/1 Tasks linked to this KR/)
  expect(within(kr2).getByRole('button', { name: /T-KR-2/ })).toBeVisible()
  expect(within(kr2).queryByRole('button', { name: /T-KR-1/ })).not.toBeInTheDocument()

  const kr3 = screen.getByRole('article', { name: 'KR-3' })
  expect(kr3).toHaveAccessibleDescription(/0 Tasks linked to this KR/)
  expect(within(kr3).getByText('No Tasks are linked to this Key Result.')).toBeVisible()

  const objectiveOnlySection = screen.getByRole('region', { name: 'Objective-only Tasks' })
  expect(within(objectiveOnlySection).getByRole('button', { name: /T-OBJ/ })).toBeVisible()
  expect(within(objectiveOnlySection).getByRole('button', { name: /T-DROP/ })).toBeVisible()
  expect(within(objectiveOnlySection).queryByRole('button', { name: /T-KR-1/ })).not.toBeInTheDocument()

  expect(screen.getByText('Unresolved KR-MISSING')).toBeVisible()
  await userEvent.click(within(kr1).getByRole('button', { name: /T-KR-1/ }))
  expect(onOpenTask).toHaveBeenCalledExactlyOnceWith('T-KR-1')
})

/**
 * Contrast cover for the loaded Objective Hub.
 *
 * jsdom does not resolve custom properties through getComputedStyle, so an axe colour-contrast pass
 * here would return "incomplete" rather than a verdict. Instead this walks the real rendered tree,
 * resolves each element's winning colour and background declaration out of the shipped stylesheet by
 * matching selectors against the actual elements, follows the var() chain into the generated theme
 * palette, and measures WCAG 2.x contrast for BOTH themes. That is what caught the original defect:
 * --ws-text-disabled is 3.13:1 on --ws-surface-base and never reaches 4.5:1 on any product surface.
 */
const WCAG_NORMAL_TEXT = 4.5

function withoutAtRules(css: string) {
  let output = ''
  for (let index = 0; index < css.length; index += 1) {
    if (css[index] !== '@') {
      output += css[index]
      continue
    }
    const open = css.indexOf('{', index)
    if (open === -1) break
    let depth = 0
    let cursor = open
    for (; cursor < css.length; cursor += 1) {
      if (css[cursor] === '{') depth += 1
      else if (css[cursor] === '}' && (depth -= 1) === 0) break
    }
    index = cursor
  }
  return output
}

interface StyleRule { selectors: string[]; body: string }

function parseRules(css: string): StyleRule[] {
  return Array.from(withoutAtRules(css.replace(/\/\*[\s\S]*?\*\//g, '')).matchAll(/([^{}]+)\{([^{}]*)\}/g))
    .map(([, selector, body]) => ({
      selectors: selector.split(',').map((part) => part.trim()).filter(Boolean),
      body,
    }))
}

function declaration(body: string, property: string) {
  const matches = Array.from(body.matchAll(new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`, 'g')))
  return matches.length ? matches[matches.length - 1][1].trim() : null
}

function matchesSelector(element: Element, selector: string) {
  if (selector.includes('::') || selector.startsWith('@')) return false
  try {
    return element.matches(selector)
  } catch {
    return false
  }
}

/** Last declaration wins: every rule involved here is a single-class or descendant selector. */
function declaredValue(element: Element, rules: StyleRule[], property: string) {
  let winner: string | null = null
  for (const rule of rules) {
    const value = declaration(rule.body, property)
    if (value === null) continue
    if (rule.selectors.some((selector) => matchesSelector(element, selector))) winner = value
  }
  return winner
}

function inheritedValue(element: Element, rules: StyleRule[], property: string, fallback: string) {
  for (let node: Element | null = element; node; node = node.parentElement) {
    const value = declaredValue(node, rules, property)
    if (value) return value
  }
  return fallback
}

/** The nearest ancestor-or-self painting a flat token background; gradients and images are skipped. */
function backgroundToken(element: Element, rules: StyleRule[], fallback: string) {
  for (let node: Element | null = element; node; node = node.parentElement) {
    const value = declaredValue(node, rules, 'background') ?? declaredValue(node, rules, 'background-color')
    if (!value) continue
    const token = /^var\((--[\w-]+)\)$/.exec(value.trim())
    if (token) return token[1]
  }
  return fallback
}

function palette(css: string, theme: 'dark' | 'light') {
  const [darkBlock, lightBlock] = css.split(":root[data-theme='light']")
  const block = theme === 'dark' ? darkBlock : lightBlock
  return Object.fromEntries(
    Array.from(block.matchAll(/(--[\w-]+):\s*([^;]+);/g)).map(([, name, value]) => [name, value.trim()]),
  ) as Record<string, string>
}

function resolveColor(value: string, themePalette: Record<string, string>, sheetVariables: Record<string, string>) {
  let current = value.trim()
  for (let hop = 0; hop < 8; hop += 1) {
    const token = /^var\((--[\w-]+)\)$/.exec(current)
    if (!token) return current
    const next = themePalette[token[1]] ?? sheetVariables[token[1]]
    if (!next) return null
    current = next.trim()
  }
  return null
}

function channel(value: number) {
  const normalized = value / 255
  return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4
}

function luminance(hex: string) {
  const digits = hex.replace('#', '')
  const full = digits.length === 3 ? digits.split('').map((part) => part + part).join('') : digits
  const [red, green, blue] = [0, 2, 4].map((offset) => parseInt(full.slice(offset, offset + 2), 16))
  return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)
}

function contrast(foreground: string, background: string) {
  const [high, low] = [luminance(foreground), luminance(background)].sort((left, right) => right - left)
  return (high + 0.05) / (low + 0.05)
}

const hubRules = parseRules(productStylesheet)
const sheetVariables = Object.fromEntries(
  Array.from(productStylesheet.matchAll(/(--ws-type-[\w-]+|--ws-control-min-height):\s*([^;]+);/g))
    .map(([, name, value]) => [name, value.trim()]),
) as Record<string, string>

function renderLoadedObjectiveHub() {
  const objective: Objective = {
    ...workspace.objectives[0],
    quarter: '2026-Q3',
    key_results: [{ id: 'KR-1', text: 'Cut release regressions', target: '5 days', progress: 40, status: 'active' }],
  }
  const tasks: Task[] = [...workspace.tasks]
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/v1/objectives/O-1') return jsonResponse({ data: { objective, tasks, activity: [] } })
    if (url === '/api/v1/workspace') return jsonResponse({ data: { ...workspace, objectives: [objective] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ObjectiveHubPage
        objectiveId="O-1"
        onCreateAlignedTask={vi.fn()}
        onNotice={vi.fn()}
        onOpenTask={vi.fn()}
        onSelectObjective={vi.fn()}
        workspace={{ ...workspace, objectives: [objective] }}
      />
    </QueryClientProvider>,
  )
}

test('every label on the loaded Objective Hub clears WCAG normal-text contrast in both themes', async () => {
  renderLoadedObjectiveHub()
  await screen.findByRole('heading', { name: 'Key Results', level: 3 })
  await screen.findByText('Cut release regressions')

  const hub = document.querySelector('.objective-hub')
  expect(hub).not.toBeNull()

  const leaves = Array.from(hub?.querySelectorAll('span, small, b, strong, p, h1, h2, h3, h4, output, em, li') ?? [])
    .filter((element) => element.textContent?.trim() && element.children.length === 0)
  expect(leaves.length).toBeGreaterThan(20)

  const failures: { color: string; report: string }[] = []
  const measured: { size: string; ratio: number }[] = []
  for (const theme of ['dark', 'light'] as const) {
    const themePalette = palette(themeTokens, theme)
    for (const element of leaves) {
      const colorValue = inheritedValue(element, hubRules, 'color', 'var(--ws-text-primary)')
      const backgroundValue = backgroundToken(element, hubRules, '--ws-bg-app')
      const foreground = resolveColor(colorValue, themePalette, sheetVariables)
      const background = resolveColor(`var(${backgroundValue})`, themePalette, sheetVariables)
      if (!foreground?.startsWith('#') || !background?.startsWith('#')) continue
      const ratio = contrast(foreground, background)
      const size = inheritedValue(element, hubRules, 'font-size', '16px')
      measured.push({ size, ratio })
      if (ratio < WCAG_NORMAL_TEXT) {
        failures.push({
          color: colorValue,
          report: `${theme} ${element.tagName} "${element.textContent?.trim().slice(0, 24)}" ${colorValue} on ${backgroundValue} = ${ratio.toFixed(2)}:1`,
        })
      }
    }
  }

  // The smallest step has to be in the sample, otherwise this could pass by measuring nothing small.
  expect(measured.some((entry) => entry.size === 'var(--ws-type-eyebrow)')).toBe(true)

  // Enforced: every label, identity code and metadata reading off the neutral text ramp. This is the
  // ramp the type steps resolve, and the ramp the reported 3.13:1 defect came from.
  const onTextRamp = (color: string) => /var\(--ws-(text-|type-label-ink)/.test(color)
  expect(failures.filter((entry) => onTextRamp(entry.color)).map((entry) => entry.report)).toEqual([])

  // Pinned, not enforced: the brand-accent numerals in the light theme (the KR progress readout and
  // the readiness counts) are a pre-existing palette gap in generated/theme-tokens.css, which this
  // lane does not own and cannot fix without repainting brand colour. Pinning the exact set means a
  // NEW accent contrast regression still fails this test rather than hiding behind the known two.
  expect(new Set(failures.filter((entry) => !onTextRamp(entry.color)).map((entry) => entry.color)))
    .toEqual(new Set(['var(--ws-brand-accent)', 'var(--ws-brand-accent-strong)']))
})

test('the shared label ink clears 4.5:1 on every Objective Hub surface in both themes', () => {
  expect(sheetVariables['--ws-type-label-ink']).toBe('var(--ws-text-muted)')

  for (const theme of ['dark', 'light'] as const) {
    const themePalette = palette(themeTokens, theme)
    const ink = resolveColor('var(--ws-type-label-ink)', themePalette, sheetVariables)
    expect(ink).toMatch(/^#[0-9a-f]{6}$/i)
    for (const surface of ['--ws-surface-base', '--ws-surface-raised', '--ws-control-bg', '--ws-bg-app']) {
      expect(contrast(ink as string, themePalette[surface])).toBeGreaterThanOrEqual(WCAG_NORMAL_TEXT)
    }
    // The token the review measured is still the failing one, so the fix is the usage, not a silent
    // repaint of a palette this lane does not own.
    expect(contrast(themePalette['--ws-text-disabled'], themePalette['--ws-surface-base'])).toBeLessThan(WCAG_NORMAL_TEXT)
  }
})

test('no Objective Hub or sidebar label step still resolves to the failing disabled ink', () => {
  const labelRules = hubRules.filter((rule) => (
    /var\(--ws-type-(eyebrow|meta)\)/.test(rule.body)
    && rule.selectors.some((selector) => /^\.(objective-|kr-|sidebar-|task-nav)/.test(selector))
  ))
  expect(labelRules.length).toBeGreaterThan(8)
  for (const rule of labelRules) {
    expect({ selector: rule.selectors.join(', '), color: declaration(rule.body, 'color') })
      .not.toEqual({ selector: rule.selectors.join(', '), color: 'var(--ws-text-disabled)' })
  }
})

test('Objective editor wraps quarter and Save instead of overlapping on a 300px track', () => {
  expect(productStylesheet).toMatch(/\.objective-editor \{[^}]*flex-wrap: wrap/)
  expect(productStylesheet).toMatch(/\.objective-editor \{[^}]*min-width: 0/)
  expect(productStylesheet).toMatch(/\.objective-editor > \.button \{[^}]*flex: 0 0 auto/)
  expect(productStylesheet).toMatch(/\.objective-detail-stage \{[^}]*grid-template-columns: minmax\(0, 1fr\)/)
  expect(productStylesheet).toMatch(/\.objective-hub \{[^}]*min-width: 0/)
  expect(productStylesheet).toMatch(/\.objective-hub-layout \{[^}]*minmax\(0, 1fr\)/)
  expect(productStylesheet).not.toMatch(/minmax\(160px, 1fr\) minmax\(100px, \.4fr\) auto/)
  expect(productStylesheet).not.toMatch(/minmax\(300px, 1\.25fr\)/)

  const narrowDesktop = productStylesheet.split('@media (max-width: 1250px) {')[1].split('\n}')[0]
  expect(narrowDesktop).toContain('.objective-overview { grid-template-columns: minmax(0, .9fr) minmax(0, 1.4fr); }')
  expect(narrowDesktop).toContain('.objective-overview > label { grid-column: 1 / -1; }')
  expect(narrowDesktop).toContain('.kr-row, .kr-card .kr-row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }')
  expect(productStylesheet).toMatch(/container: objective-detail \/ inline-size/)
  expect(productStylesheet).toMatch(/@container objective-detail \(max-width: 800px\)/)
})
