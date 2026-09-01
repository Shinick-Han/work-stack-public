import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, vi } from 'vitest'
import { App } from './App'
import { api, ApiError } from '../api/client'
import type { Capture, Task, WorkspaceProjection } from '../domain/types'
import { capture, jsonResponse, task, workspace } from '../test/fixtures'

beforeEach(() => {
  window.localStorage.clear()
  document.documentElement.dataset.theme = 'dark'
  Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
  vi.spyOn(api, 'getSyncStatus').mockRejectedValue(new ApiError(404, 'not_found', 'Optional legacy endpoint'))
})

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function response(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubWorkspaceFetch() {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: workspace })
    if (url.includes('/api/v1/review')) return jsonResponse({ data: {
      day: { date: '2026-08-30', start_time: null, entries: [] },
      weekly: { range: { start: '2026-08-24', end: '2026-08-30', days: 7 }, objectives: [], projects: [] },
    } })
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    if (url.includes('/api/v1/objectives/O-1')) return jsonResponse({ data: {
      objective: workspace.objectives[0],
      tasks: workspace.tasks,
      activity: [],
    } })
    if (url.includes('/api/v1/tasks/T-0001')) return jsonResponse({
      data: { task, context: [], activity: [], replies: [] },
    })
    throw new Error(`Unexpected request: ${url}`)
  }))
}

function renderApp(props: Parameters<typeof App>[0] = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    client,
    ...render(<QueryClientProvider client={client}><App {...props} /></QueryClientProvider>),
  }
}

test('switches between dark and light product themes and remembers the choice', async () => {
  stubWorkspaceFetch()
  renderApp()

  await screen.findByRole(
    'heading',
    { name: /keep execution connected/i },
    { timeout: 5_000 },
  )
  expect(document.documentElement.dataset.theme).toBe('dark')

  await userEvent.click(screen.getByRole('button', { name: 'Use light theme' }))
  expect(document.documentElement.dataset.theme).toBe('light')
  expect(window.localStorage.getItem('workstack.theme')).toBe('light')
  expect(screen.getByRole('button', { name: 'Use dark theme' })).toBeInTheDocument()
})

function stubQuickTaskCreation(
  createdTask: Task,
  options: {
    captures?: Capture[]
    failWorkspaceRefresh?: boolean
    postPayload?: unknown
  } = {},
) {
  const counters = { post: 0, workspace: 0 }
  let committed = false
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    if (url.endsWith('/api/v1/tasks') && init?.method === 'POST') {
      counters.post += 1
      committed = true
      return jsonResponse({ data: options.postPayload ?? createdTask, meta: { replayed: false } }, 201)
    }
    if (url.includes(`/api/v1/tasks/${createdTask.id}`)) return jsonResponse({
      data: { task: createdTask, context: [], activity: [], replies: [] },
    })
    if (url.includes('/api/v1/workspace')) {
      counters.workspace += 1
      if (options.failWorkspaceRefresh && counters.workspace > 1) {
        return jsonResponse({
          error: { code: 'workspace_unavailable', message: 'workspace refresh unavailable' },
        }, 503)
      }
      return jsonResponse({
        data: committed ? { ...workspace, tasks: [...workspace.tasks, createdTask] } : workspace,
      })
    }
    if (url.includes('/api/v1/captures')) {
      return jsonResponse({ data: { captures: options.captures ?? [] } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  return counters
}

async function submitQuickTask(title: string) {
  await userEvent.click(screen.getByRole('button', { name: 'New task' }))
  await userEvent.type(screen.getByRole('textbox', { name: 'Task title' }), title)
  await userEvent.click(screen.getByRole('button', { name: 'Create task' }))
}

test('moves between the Workspace and Context Inbox product surfaces', async () => {
  stubWorkspaceFetch()
  renderApp()

  expect(await screen.findByRole(
    'heading',
    { name: /keep execution connected/i },
    { timeout: 5_000 },
  )).toBeInTheDocument()
  expect(screen.getByText('Local workspace')).toBeInTheDocument()
  expect(screen.getByText('On this device · no background sync')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Configure SSOT connection' })).not.toBeInTheDocument()
  expect(screen.queryByText('Healthy')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /context inbox/i }))
  expect(await screen.findByRole('heading', { name: /turn signal into useful work/i })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Copy Microsoft 365 request' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Import agent result' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Import packet' })).toBeEnabled()
  expect(screen.queryByText(/Microsoft 365 guided handoff unavailable/i)).not.toBeInTheDocument()
  expect(window.location.search).toContain('surface=inbox')
})

test('replaces the passive sidebar status with one SSOT configuration entry in the desktop host', async () => {
  stubWorkspaceFetch()
  Object.defineProperty(window, 'chrome', {
    configurable: true,
    value: {
      webview: {
        addEventListener: vi.fn(),
        postMessage: vi.fn(),
        removeEventListener: vi.fn(),
      },
    },
  })
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  expect(screen.getAllByRole('button', { name: 'Configure SSOT connection' })).toHaveLength(1)
  expect(screen.queryByText('On this device · no background sync')).not.toBeInTheDocument()
})

test('routes both SSOT entry points to one multi-profile center only when its gate is enabled', async () => {
  stubWorkspaceFetch()
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', {
    configurable: true,
    value: { webview: { addEventListener: vi.fn(), postMessage, removeEventListener: vi.fn() } },
  })
  renderApp({ connectionCenterGates: { registry: true, activation: false } })

  await screen.findByRole('heading', { name: /keep execution connected/i })
  const entryPoints = screen.getAllByRole('button', { name: /Configure SSOT connections/ })
  expect(entryPoints).toHaveLength(2)
  expect(screen.queryByRole('button', { name: 'Configure SSOT connection' })).not.toBeInTheDocument()

  await userEvent.click(entryPoints[0])
  expect(screen.getAllByRole('dialog', { name: 'SSOT connections' })).toHaveLength(1)
  expect(screen.getByRole('button', { name: 'Save and activate after restart' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: 'Close' }))
  expect(screen.queryByRole('dialog', { name: 'SSOT connections' })).not.toBeInTheDocument()

  await userEvent.click(entryPoints[1])
  expect(screen.getAllByRole('dialog', { name: 'SSOT connections' })).toHaveLength(1)
  expect(postMessage).toHaveBeenCalled()
})

test('keeps a searchable Task navigator collapsed until requested and opens its Task in Workspace', async () => {
  stubWorkspaceFetch()
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  expect(screen.getByRole('searchbox', { name: 'Filter sidebar tasks', hidden: true })).not.toBeVisible()

  await userEvent.click(screen.getByRole('button', { name: 'Tasks 1' }))
  const search = screen.getByRole('searchbox', { name: 'Filter sidebar tasks' })
  expect(search).toBeVisible()
  await userEvent.type(search, 'release quality')
  const taskLink = screen.getByRole('button', { name: /Open task T-0001: Define release quality gate/i })
  await userEvent.click(taskLink)

  expect(await screen.findByRole('complementary', { name: 'Task T-0001' })).toBeInTheDocument()
  expect(window.location.search).toContain('task=T-0001')
})

test('opens workspace actions and creates a Context card through the versioned API', async () => {
  let posted: Record<string, unknown> | null = null
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    if (url.endsWith('/api/v1/notes') && init?.method === 'POST') {
      posted = JSON.parse(String(init.body)) as Record<string, unknown>
      return jsonResponse({ data: { id: 'N-0099', text: posted.text, links: posted.links, created: '2026-08-30' }, meta: { replayed: false } }, 201)
    }
    if (url.includes('/api/v1/storage')) return jsonResponse({ data: {
      workspace_id: workspace.workspace.id,
      store_schema_version: 3,
      product_version: '1.0.0',
      remote_protocol_version: 1,
      file_count: 9,
      total_bytes: 4096,
      backup_format: 'workstack-backup-v1',
      restore_requires_shutdown: true,
    } })
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: workspace })
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await userEvent.click(screen.getByRole('button', { name: 'New task' }))
  const quickTaskDialog = await screen.findByRole('dialog', { name: 'New task' })
  await userEvent.type(within(quickTaskDialog).getByRole('textbox', { name: 'Task title' }), 'Preserve this Task draft')
  await userEvent.click(within(quickTaskDialog).getByRole('button', { name: 'Close' }))
  await userEvent.click(screen.getByRole('button', { name: 'More workspace actions' }))
  const dialog = await screen.findByRole('dialog', { name: 'Workspace actions' })
  expect(within(dialog).queryByRole('button', { name: 'Add objective' })).not.toBeInTheDocument()
  await userEvent.type(within(dialog).getByRole('textbox', { name: 'Context card' }), 'Keep planning friction visible')
  await userEvent.click(within(dialog).getByRole('button', { name: 'Add context card' }))

  await waitFor(() => expect(posted).toMatchObject({ text: 'Keep planning friction visible', links: [] }))
  expect(await screen.findByText('Context card N-0099 added')).toBeInTheDocument()
  expect(screen.queryByRole('dialog', { name: 'Workspace actions' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'New task' }))
  expect(within(await screen.findByRole('dialog', { name: 'New task' })).getByRole('textbox', { name: 'Task title' })).toHaveValue('Preserve this Task draft')
})

test('clears the local Quick Add draft only after a confirmed Task creation', async () => {
  const createdTask: Task = {
    ...task,
    id: 'T-0099',
    uid: '99999999-9999-4999-8999-999999999999',
    title: 'Clear this committed draft',
    revision: 0,
  }
  stubQuickTaskCreation(createdTask)
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await submitQuickTask(createdTask.title)
  expect(await screen.findByRole('complementary', { name: `Task ${createdTask.id}` })).toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: 'New task' }))
  expect(within(await screen.findByRole('dialog', { name: 'New task' })).getByRole('textbox', { name: 'Task title' })).toHaveValue('')
})

test('opens the Focus surface and reuses the existing task drawer', async () => {
  stubWorkspaceFetch()
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await userEvent.click(within(screen.getByRole('navigation', { name: 'Product surfaces' })).getByRole('button', { name: /^Focus/ }))

  expect(await screen.findByRole('heading', { name: /deserves attention now/i })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /^Focus/ })).toHaveAttribute('aria-current', 'page')
  expect(screen.getByText('In progress')).toBeInTheDocument()
  expect(screen.getByText('P0', { selector: '.pill' })).toBeInTheDocument()
  expect(window.location.search).toContain('surface=focus')

  const slash = new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true })
  window.dispatchEvent(slash)
  expect(slash.defaultPrevented).toBe(false)

  await userEvent.click(screen.getByRole('button', { name: `Open ${task.id} · ${task.title}` }))
  expect(window.location.search).toContain(`task=${task.id}`)
  expect(await screen.findByText('Make release criteria measurable.')).toBeInTheDocument()
})

test('opens Quick Add from Objective Hub with only that Objective alignment replaced', async () => {
  stubWorkspaceFetch()
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await userEvent.click(screen.getByRole('button', { name: /^Objective Hub/ }))
  expect(await screen.findByRole('heading', { name: /goal–work chain/i })).toBeVisible()
  await userEvent.click(await screen.findByRole('button', { name: 'Create aligned task' }))

  const dialog = await screen.findByRole('dialog', { name: 'New task' })
  expect(within(dialog).getByLabelText(/Objective/)).toHaveValue('O-1')
})

test('uses 1–8 for views and surfaces and J/K for filtered task navigation', async () => {
  const secondTask: Task = {
    ...task,
    id: 'T-0002',
    uid: '22222222-2222-4222-8222-222222222222',
    title: 'Second navigable task',
  }
  const shortcutWorkspace = { ...workspace, tasks: [task, secondTask] }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: shortcutWorkspace })
    if (url.includes('/api/v1/review')) return jsonResponse({ data: {
      day: { date: '2026-08-30', start_time: null, entries: [] },
      weekly: { range: { start: '2026-08-24', end: '2026-08-30', days: 7 }, objectives: [], projects: [] },
    } })
    if (url.includes('/api/v1/objectives/O-1')) return jsonResponse({ data: {
      objective: shortcutWorkspace.objectives[0], tasks: shortcutWorkspace.tasks, activity: [],
    } })
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    if (url.includes('/api/v1/tasks/')) {
      const selected = url.includes('T-0002') ? secondTask : task
      return jsonResponse({ data: { task: selected, context: [], activity: [], replies: [] } })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
  renderApp()
  await screen.findByRole('heading', { name: /keep execution connected/i })

  fireEvent.keyDown(window, { key: '2' })
  expect(screen.getByRole('tab', { name: 'Board' })).toHaveAttribute('aria-selected', 'true')
  fireEvent.keyDown(window, { key: '3' })
  expect(screen.getByRole('tab', { name: 'Treemap' })).toHaveAttribute('aria-selected', 'true')
  fireEvent.keyDown(window, { key: '4' })
  expect(await screen.findByRole('button', { name: 'Focus 4' })).toHaveAttribute('aria-current', 'page')
  fireEvent.keyDown(window, { key: '5' })
  expect(await screen.findByRole('heading', { name: /turn signal into useful work/i })).toBeInTheDocument()
  fireEvent.keyDown(window, { key: '6' })
  expect(await screen.findByRole('heading', { name: /turn execution into evidence/i })).toBeInTheDocument()
  fireEvent.keyDown(window, { key: '7' })
  expect(await screen.findByRole('heading', { name: /make the goal–work chain explicit/i })).toBeInTheDocument()
  fireEvent.keyDown(window, { key: '8' })
  expect(await screen.findByRole('tab', { name: 'Table' })).toHaveAttribute('aria-selected', 'true')
  fireEvent.keyDown(window, { key: '1' })
  expect(await screen.findByRole('tab', { name: 'Graph' })).toHaveAttribute('aria-selected', 'true')

  fireEvent.keyDown(window, { key: 'k' })
  expect(await screen.findByRole('complementary', { name: 'Task T-0002' })).toBeInTheDocument()
  fireEvent.keyDown(window, { key: 'j' })
  expect(await screen.findByRole('complementary', { name: 'Task T-0001' })).toBeInTheDocument()
})

test('moves from a Capture to Workspace when opening its newly created Task', async () => {
  const createdTask = {
    ...task,
    id: 'T-0042',
    uid: '42424242-4242-4242-8242-424242424242',
    title: capture.source.display_title,
    detail: capture.normalized.context,
    priority: 'P2' as const,
    due: null,
    objective_ids: [],
    revision: 0,
  }
  let created = false
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    if (url.includes('/api/v1/captures/C-0001/task') && init?.method === 'POST') {
      created = true
      return jsonResponse({ data: { task: createdTask } })
    }
    if (url.includes('/api/v1/tasks/T-0042')) return jsonResponse({
      data: { task: createdTask, context: [], activity: [], replies: [] },
    })
    if (url.includes('/api/v1/workspace')) return jsonResponse({
      data: created ? { ...workspace, tasks: [...workspace.tasks, createdTask] } : workspace,
    })
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [capture] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await userEvent.click(screen.getByRole('button', { name: /context inbox/i }))
  await screen.findByRole('heading', { name: /turn signal into useful work/i })
  await userEvent.click(screen.getByRole('button', { name: 'Inspect C-0001' }))

  const captureDrawer = screen.getByRole('complementary', { name: 'Capture C-0001' })
  await userEvent.click(within(captureDrawer).getByRole('button', { name: 'Create task from this source' }))
  await userEvent.click(within(captureDrawer).getByRole('button', { name: 'Create linked task' }))

  const taskDrawer = await screen.findByRole('complementary', { name: 'Task T-0042' })
  expect(within(taskDrawer).getByRole('textbox', { name: 'Task title' })).toHaveValue(capture.source.display_title)
  expect(window.location.search).toBe('?task=T-0042')
  expect(screen.getByRole('button', { name: /Workspace 1/ })).toHaveAttribute('aria-current', 'page')
})

test('opens a Quick Add Task immediately even when the background Workspace refresh fails', async () => {
  const createdTask: Task = {
    ...task,
    id: 'T-0043',
    uid: '43434343-4343-4343-8343-434343434343',
    title: 'Continue from Quick Add',
    revision: 0,
  }
  const counters = stubQuickTaskCreation(createdTask, { failWorkspaceRefresh: true })
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await submitQuickTask(createdTask.title)

  const drawer = await screen.findByRole('complementary', { name: `Task ${createdTask.id}` })
  expect(await within(drawer).findByRole('textbox', { name: 'Task title' })).toHaveValue(createdTask.title)
  expect(window.location.search).toBe(`?task=${createdTask.id}`)
  expect(screen.getByText(`${createdTask.id} created`)).toBeInTheDocument()
  await waitFor(() => expect(counters.workspace).toBe(2))
  expect(screen.getByRole('status', { name: 'Workspace refresh failed' })).toHaveTextContent('workspace refresh unavailable')
  expect(screen.getByRole('button', { name: 'Retry workspace refresh' })).toBeEnabled()
  expect(screen.getByRole('heading', { name: /keep execution connected/i })).toBeInTheDocument()
  expect(counters.post).toBe(1)
})

test('keeps Focus selected when Quick Add opens the created Task', async () => {
  const createdTask: Task = {
    ...task,
    id: 'T-0044',
    uid: '44444444-4444-4444-8444-444444444444',
    title: 'Protect the Focus surface',
    revision: 0,
  }
  const counters = stubQuickTaskCreation(createdTask)
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await userEvent.click(within(screen.getByRole('navigation', { name: 'Product surfaces' })).getByRole('button', { name: /^Focus/ }))
  await screen.findByRole('heading', { name: /deserves attention now/i })
  await submitQuickTask(createdTask.title)

  expect(await screen.findByRole('complementary', { name: `Task ${createdTask.id}` })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /^Focus/ })).toHaveAttribute('aria-current', 'page')
  expect(window.location.search).toContain('surface=focus')
  expect(window.location.search).toContain(`task=${createdTask.id}`)
  expect(counters.post).toBe(1)
})

test('moves Inbox Quick Add to Workspace and clears the Capture drawer', async () => {
  const createdTask: Task = {
    ...task,
    id: 'T-0045',
    uid: '45454545-4545-4545-8545-454545454545',
    title: 'Turn Inbox context into work',
    revision: 0,
  }
  const counters = stubQuickTaskCreation(createdTask, { captures: [capture] })
  window.history.replaceState(null, '', `/?surface=inbox&capture=${capture.id}`)
  renderApp()

  expect(await screen.findByRole('complementary', { name: `Capture ${capture.id}` })).toBeInTheDocument()
  fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
  const commandDialog = await screen.findByRole('dialog', { name: 'Search or jump' })
  await userEvent.click(within(commandDialog).getByRole('option', { name: /Create a new task/i }))
  const quickDialog = await screen.findByRole('dialog', { name: 'New task' })
  await userEvent.type(within(quickDialog).getByRole('textbox', { name: 'Task title' }), createdTask.title)
  await userEvent.click(within(quickDialog).getByRole('button', { name: 'Create task' }))

  expect(await screen.findByRole('complementary', { name: `Task ${createdTask.id}` })).toBeInTheDocument()
  expect(screen.queryByRole('complementary', { name: `Capture ${capture.id}` })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: /^Workspace/ })).toHaveAttribute('aria-current', 'page')
  expect(window.location.search).toBe(`?task=${createdTask.id}`)
  await waitFor(() => expect(counters.workspace).toBe(2))
  expect(counters.post).toBe(1)
})

test('treats an invalid committed response as unknown and prevents an immediate duplicate submit', async () => {
  const createdTask: Task = {
    ...task,
    id: 'T-0046',
    uid: '46464646-4646-4646-8646-464646464646',
    title: 'Potentially committed task',
    revision: 0,
  }
  const counters = stubQuickTaskCreation(createdTask, { postPayload: { unexpected: true } })
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await submitQuickTask(createdTask.title)

  expect(await screen.findByText('Task may have been created. Check Workspace before trying again.')).toBeInTheDocument()
  expect(screen.queryByRole('dialog', { name: 'New task' })).not.toBeInTheDocument()
  expect(screen.queryByRole('complementary', { name: `Task ${createdTask.id}` })).not.toBeInTheDocument()
  expect(screen.queryByText(`${createdTask.id} created`)).not.toBeInTheDocument()
  await waitFor(() => expect(counters.workspace).toBe(2))
  expect(counters.post).toBe(1)
  expect(window.location.search).toBe('')
})

test('preserves the Quick Add draft when cancel races the pending render and the request fails', async () => {
  const taskPost = deferred<Response>()
  let postCalls = 0
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    if (url.endsWith('/api/v1/tasks') && init?.method === 'POST') {
      postCalls += 1
      return taskPost.promise
    }
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: workspace })
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await userEvent.click(screen.getByRole('button', { name: 'New task' }))
  const dialog = await screen.findByRole('dialog')
  const title = within(dialog).getByRole('textbox', { name: 'Task title' })
  await userEvent.type(title, 'Keep this failed draft')

  const form = document.querySelector<HTMLFormElement>('#quick-task-form')!
  const cancelEvent = new Event('cancel', { bubbles: true, cancelable: true })
  act(() => {
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    dialog.dispatchEvent(cancelEvent)
  })

  expect(cancelEvent.defaultPrevented).toBe(true)
  expect(dialog).toHaveAttribute('open')
  expect(title).toHaveValue('Keep this failed draft')
  await waitFor(() => expect(postCalls).toBe(1))

  taskPost.resolve(response({
    error: { code: 'invalid_request', message: 'task rejected' },
  }, 400))

  expect(await within(dialog).findByRole('alert')).toHaveTextContent('task rejected')
  expect(title).toHaveValue('Keep this failed draft')
  expect(title).toBeEnabled()
  expect(postCalls).toBe(1)
})

test('keeps a Board status change pending through its request and applies the returned revision', async () => {
  const patch = deferred<Response>()
  let patchBody: Record<string, unknown> | null = null
  let patchCalls = 0
  let workspaceReads = 0
  let serverTask: Task = task

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    }
    if (url.includes('/api/v1/tasks/T-0001') && init?.method === 'PATCH') {
      patchCalls += 1
      patchBody = JSON.parse(String(init.body)) as Record<string, unknown>
      return patch.promise
    }
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    }
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const { client } = renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await userEvent.click(screen.getByRole('tab', { name: 'Board' }))
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Change T-0001 status' }), 'done')

  await waitFor(() => {
    expect(screen.getByRole('combobox', { name: 'Change T-0001 status' })).toBeDisabled()
  })
  expect(patchCalls).toBe(1)
  expect(patchBody).toEqual({ status: 'done', revision: task.revision })
  expect(workspaceReads).toBe(1)

  serverTask = { ...task, status: 'done', revision: task.revision + 1 }
  patch.resolve(response({ data: serverTask }))

  await waitFor(() => expect(workspaceReads).toBe(2))
  await waitFor(() => {
    expect(screen.getByRole('combobox', { name: 'Change T-0001 status' })).toBeEnabled()
  })
  expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks[0]).toMatchObject({
    id: task.id,
    status: 'done',
    revision: task.revision + 1,
  })
  expect(screen.getByText(`${task.id} moved to done`)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
})

test('undoes only the latest status change through a new revision-guarded fact', async () => {
  const patchBodies: Record<string, unknown>[] = []
  let serverTask: Task = task
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    if (url.includes('/api/v1/tasks/T-0001') && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>
      patchBodies.push(body)
      serverTask = {
        ...serverTask,
        revision: serverTask.revision + 1,
        status: body.status as Task['status'],
      }
      return response({ data: serverTask })
    }
    if (url.includes('/api/v1/workspace')) return jsonResponse({ data: { ...workspace, tasks: [serverTask] } })
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const { client } = renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await userEvent.click(screen.getByRole('tab', { name: 'Board' }))
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Change T-0001 status' }), 'done')
  await userEvent.click(await screen.findByRole('button', { name: 'Undo' }))

  await waitFor(() => expect(patchBodies).toHaveLength(2))
  expect(patchBodies).toEqual([
    { status: 'done', revision: task.revision },
    { status: task.status, revision: task.revision + 1 },
  ])
  await waitFor(() => expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks[0]).toMatchObject({
    status: task.status,
    revision: task.revision + 2,
  }))
  expect(screen.getByText(`${task.id} restored to ${task.status}`)).toBeInTheDocument()
})

test('returns every workspace view to the unselected state when the active task is clicked again', async () => {
  window.history.replaceState(null, '', `/?view=board&task=${task.id}`)
  stubWorkspaceFetch()
  renderApp()

  expect(await screen.findByRole('complementary', { name: `Task ${task.id}` })).toBeInTheDocument()
  await userEvent.click(screen.getByLabelText(`${task.id}: ${task.title}`))

  await waitFor(() => {
    expect(screen.queryByRole('complementary', { name: `Task ${task.id}` })).not.toBeInTheDocument()
  })
  expect(window.location.search).toBe('?view=board')
  expect(screen.getByRole('tabpanel', { name: 'board workspace view' })).toBeInTheDocument()
})

test('rolls back only a conflicting Task and preserves a concurrent successful status change', async () => {
  const secondTask: Task = {
    ...task,
    id: 'T-0002',
    uid: '33333333-3333-3333-8333-333333333333',
    title: 'Preserve concurrent delivery work',
    status: 'open',
    revision: 7,
  }
  const firstPatch = deferred<Response>()
  const secondPatch = deferred<Response>()
  const patchBodies = new Map<string, Record<string, unknown>>()
  let workspaceReads = 0
  let serverWorkspace: WorkspaceProjection = { ...workspace, tasks: [task, secondTask] }

  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.includes('/api/v1/session')) {
      return jsonResponse({ data: { csrf_token: 'test-csrf-token' } })
    }
    if (init?.method === 'PATCH') {
      const taskId = url.includes('/T-0002') ? 'T-0002' : 'T-0001'
      patchBodies.set(taskId, JSON.parse(String(init.body)) as Record<string, unknown>)
      return taskId === 'T-0001' ? firstPatch.promise : secondPatch.promise
    }
    if (url.includes('/api/v1/workspace')) {
      workspaceReads += 1
      return jsonResponse({ data: serverWorkspace })
    }
    if (url.includes('/api/v1/captures')) return jsonResponse({ data: { captures: [] } })
    throw new Error(`Unexpected request: ${url}`)
  }))
  const { client } = renderApp()

  await screen.findByRole('heading', { name: /keep execution connected/i })
  await userEvent.click(screen.getByRole('tab', { name: 'Board' }))
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Change T-0001 status' }), 'done')
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Change T-0002 status' }), 'done')

  await waitFor(() => expect(patchBodies.size).toBe(2))
  expect(patchBodies.get('T-0001')).toEqual({ status: 'done', revision: task.revision })
  expect(patchBodies.get('T-0002')).toEqual({ status: 'done', revision: secondTask.revision })
  expect(workspaceReads).toBe(1)

  const updatedSecondTask = { ...secondTask, status: 'done' as const, revision: secondTask.revision + 1 }
  serverWorkspace = { ...workspace, tasks: [task, updatedSecondTask] }
  secondPatch.resolve(response({ data: updatedSecondTask }))

  await waitFor(() => {
    expect(client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks.find((item) => item.id === secondTask.id)).toMatchObject({
      status: 'done',
      revision: secondTask.revision + 1,
    })
  })
  expect(workspaceReads).toBe(1)

  const refreshedFirstTask = { ...task, status: 'started' as const, revision: task.revision + 1 }
  serverWorkspace = { ...workspace, tasks: [refreshedFirstTask, updatedSecondTask] }
  firstPatch.resolve(response({
    error: {
      code: 'revision_conflict',
      message: 'task revision is stale',
      details: { expected: task.revision + 1, received: task.revision },
    },
  }, 409))

  await waitFor(() => expect(workspaceReads).toBe(2))
  const finalTasks = client.getQueryData<WorkspaceProjection>(['workspace'])?.tasks ?? []
  expect(finalTasks.find((item) => item.id === task.id)).toMatchObject({
    status: 'started',
    revision: task.revision + 1,
  })
  expect(finalTasks.find((item) => item.id === secondTask.id)).toMatchObject({
    status: 'done',
    revision: secondTask.revision + 1,
  })
  expect(screen.getByRole('combobox', { name: 'Change T-0001 status' })).toHaveValue('started')
  expect(screen.getAllByText('task revision is stale').length).toBeGreaterThan(0)
})
