import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import { api } from '../../../api/client'
import { taskDetailSchema } from '../../../domain/schemas'
import type { TaskDetail } from '../../../domain/types'
import { capture, task } from '../../../test/fixtures'
import { GraphContextPopover } from './GraphContextPopover'

function detail(id = task.id, content = true): TaskDetail {
  return taskDetailSchema.parse({
    task: { ...task, id }, activity: [], replies: [],
    context: content ? [
      { ...capture, id: 'same', ref: { kind: 'capture', id: 'same' }, connections: [], date_precision: 'instant' },
      { id: 'same', text: `Note for ${id}`, links: [id], created: '2026-09-02',
        ref: { kind: 'note', id: 'same' }, connections: [], date_precision: 'date' },
    ] : [],
  }) as TaskDetail
}

const triggers: HTMLButtonElement[] = []
afterEach(() => { for (const trigger of triggers.splice(0)) trigger.remove() })
function setup(id = task.id) {
  const trigger = document.createElement('button')
  trigger.textContent = 'Context trigger'
  document.body.append(trigger)
  triggers.push(trigger)
  trigger.focus()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const onClose = vi.fn()
  const onOpenTask = vi.fn()
  const props = { taskId: id, taskTitle: `Task ${id}`, trigger, onClose, onOpenTask }
  const view = render(<QueryClientProvider client={client}><GraphContextPopover {...props} /></QueryClientProvider>)
  return { ...view, client, props, trigger, onClose, onOpenTask,
    changeTask(next: string) { view.rerender(<QueryClientProvider client={client}><GraphContextPopover {...props} taskId={next} taskTitle={`Task ${next}`} /></QueryClientProvider>) },
  }
}

test('queries the selected task while loading, then preserves typed cards, order, dates and source gates', async () => {
  let resolve!: (value: TaskDetail) => void
  const request = vi.spyOn(api, 'getTask').mockReturnValue(new Promise((done) => { resolve = done }))
  const view = setup()
  expect(screen.getByRole('status')).toHaveTextContent('Loading context')
  expect(request).toHaveBeenCalledExactlyOnceWith(task.id)
  expect(view.client.getQueryState(['task', task.id])).toBeDefined()
  await act(async () => resolve(detail()))
  const dialog = screen.getByRole('dialog', { name: `Task ${task.id}` })
  await within(dialog).findByRole('heading', { name: 'Release review feedback' })
  expect(within(dialog).getAllByRole('article').map((article) => within(article).getByRole('heading').textContent))
    .toEqual(['Release review feedback', `Note for ${task.id}`])
  expect(screen.getByText('2026-09-02')).toHaveAttribute('datetime', '2026-09-02')
  expect(screen.getByText('Reply unavailable · Gate 0 pending')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Open source/ })).toHaveAttribute('href', capture.source.web_url)
})

test('shows a truthful error, retries explicitly and renders returned empty context', async () => {
  const request = vi.spyOn(api, 'getTask').mockRejectedValueOnce(new Error('private server detail')).mockResolvedValue(detail(task.id, false))
  setup()
  expect(await screen.findByRole('alert')).toHaveTextContent('Context could not be loaded')
  expect(screen.queryByText('private server detail')).not.toBeInTheDocument()
  expect(request).toHaveBeenCalledTimes(1)
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('No context yet')).toBeInTheDocument()
  expect(request).toHaveBeenCalledTimes(2)
})

test('late A cannot appear under B or reopen a closed panel', async () => {
  let resolveA!: (value: TaskDetail) => void
  let resolveB!: (value: TaskDetail) => void
  vi.spyOn(api, 'getTask').mockImplementation((id) => new Promise((resolve) => {
    if (id === 'T-0001') resolveA = resolve
    else resolveB = resolve
  }))
  const view = setup('T-0001')
  view.changeTask('T-0002')
  await act(async () => resolveA(detail('T-0001')))
  expect(screen.getByRole('dialog', { name: 'Task T-0002' })).toBeInTheDocument()
  expect(screen.queryByText('Note for T-0001')).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Loading context')
  view.unmount()
  await act(async () => resolveB(detail('T-0002')))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(view.trigger).toHaveFocus()
})

test('refuses a result bound to a different task', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue(detail('T-0002'))
  setup('T-0001')
  expect(await screen.findByRole('alert')).toHaveTextContent('Context could not be loaded')
  expect(screen.queryByText('Note for T-0002')).not.toBeInTheDocument()
})

test('contains keyboard focus, closes with Escape and restores the surviving trigger', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue(detail(task.id, false))
  const view = setup()
  await screen.findByText('No context yet')
  expect(screen.getByRole('button', { name: 'Close context' })).toHaveFocus()
  await userEvent.tab({ shift: true })
  expect(screen.getByRole('button', { name: 'Open task' })).toHaveFocus()
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Close context' })).toHaveFocus()
  await userEvent.keyboard('{Escape}')
  expect(view.onClose).toHaveBeenCalledOnce()
  view.unmount()
  expect(view.trigger).toHaveFocus()
})

test('explicit task opening is an action, and a removed trigger does not break cleanup', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue(detail(task.id, false))
  const view = setup()
  await screen.findByText('No context yet')
  await userEvent.click(screen.getByRole('button', { name: 'Open task' }))
  expect(view.onOpenTask).toHaveBeenCalledOnce()
  view.trigger.remove()
  expect(() => view.unmount()).not.toThrow()
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})

test('falls focus back to a stable Workspace element when the trigger is gone', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue(detail())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })

  const fallback = document.createElement('div')
  fallback.className = 'wsv-graph-fallback'
  document.body.appendChild(fallback)
  const detached = document.createElement('button')
  const onFocusReturned = vi.fn()

  const view = render(<QueryClientProvider client={client}><GraphContextPopover
    taskId={task.id}
    taskTitle={task.title}
    trigger={detached}
    focusFallbackSelector=".wsv-graph-fallback"
    onClose={vi.fn()}
    onOpenTask={vi.fn()}
    onFocusReturned={onFocusReturned}
  /></QueryClientProvider>)
  await screen.findByRole('dialog', { name: task.title })

  // The trigger was never connected, so focus must not be dropped on the body.
  view.unmount()
  expect(fallback).toHaveFocus()
  expect(onFocusReturned).toHaveBeenCalledTimes(1)
  fallback.remove()
})

test('restores a connected trigger and only then releases the focus pin', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue(detail())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })

  const trigger = document.createElement('button')
  document.body.appendChild(trigger)
  const onFocusReturned = vi.fn()

  const view = render(<QueryClientProvider client={client}><GraphContextPopover
    taskId={task.id}
    taskTitle={task.title}
    trigger={trigger}
    focusFallbackSelector=".wsv-graph-fallback"
    onClose={vi.fn()}
    onOpenTask={vi.fn()}
    onFocusReturned={onFocusReturned}
  /></QueryClientProvider>)
  await screen.findByRole('dialog', { name: task.title })
  // Still open: the pin has not been released yet.
  expect(onFocusReturned).not.toHaveBeenCalled()

  view.unmount()
  // Corrected F1 contract: focus returns to the connected trigger and the pin
  // stays with it. It is released only once focus ACTUALLY leaves.
  expect(trigger).toHaveFocus()
  expect(onFocusReturned).not.toHaveBeenCalled()

  trigger.dispatchEvent(new FocusEvent('blur'))
  expect(onFocusReturned).toHaveBeenCalledTimes(1)
  trigger.remove()
})

test('accepts a null trigger without throwing', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue(detail())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const view = render(<QueryClientProvider client={client}><GraphContextPopover
    taskId={task.id}
    taskTitle={task.title}
    trigger={null}
    onClose={vi.fn()}
    onOpenTask={vi.fn()}
  /></QueryClientProvider>)
  await screen.findByRole('dialog', { name: task.title })
  expect(() => view.unmount()).not.toThrow()
})

test('CF4 a stale observer from an unmounted owner cannot focus a later Workspace', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue(detail())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })

  // The old owner: a trigger that will be detached after the popup closes.
  const oldOwner = document.createElement('div')
  const trigger = document.createElement('button')
  const oldFallback = document.createElement('div')
  oldFallback.setAttribute('data-workspace-focus-fallback', '')
  oldOwner.append(trigger, oldFallback)
  document.body.appendChild(oldOwner)

  const view = render(<QueryClientProvider client={client}><GraphContextPopover
    taskId={task.id}
    taskTitle={task.title}
    trigger={trigger}
    focusFallbackSelector="[data-workspace-focus-fallback]"
    onClose={vi.fn()}
    onOpenTask={vi.fn()}
    onFocusReturned={vi.fn()}
  /></QueryClientProvider>)
  await screen.findByRole('dialog', { name: task.title })

  view.unmount()
  expect(trigger).toHaveFocus()

  // The whole old owner goes away, then a NEW Workspace mounts and its own
  // control is deliberately focused.
  oldOwner.remove()
  const newOwner = document.createElement('div')
  const newRefresh = document.createElement('button')
  newRefresh.setAttribute('data-workspace-focus-fallback', '')
  newOwner.appendChild(newRefresh)
  document.body.appendChild(newOwner)
  newRefresh.focus()
  expect(newRefresh).toHaveFocus()

  // Deliver any queued MutationObserver callbacks from the dead owner.
  await act(async () => { await Promise.resolve() })
  await new Promise((resolve) => setTimeout(resolve, 0))

  // A stale callback must not steal focus into the new owner.
  expect(newRefresh).toHaveFocus()
  newOwner.remove()
})

test('CF4 control: a real blur before unmount settles the watch and leaves the new control focused', async () => {
  vi.spyOn(api, 'getTask').mockResolvedValue(detail())
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const oldOwner = document.createElement('div')
  const trigger = document.createElement('button')
  oldOwner.appendChild(trigger)
  document.body.appendChild(oldOwner)
  const onFocusReturned = vi.fn()

  const view = render(<QueryClientProvider client={client}><GraphContextPopover
    taskId={task.id}
    taskTitle={task.title}
    trigger={trigger}
    focusFallbackSelector="[data-workspace-focus-fallback]"
    onClose={vi.fn()}
    onOpenTask={vi.fn()}
    onFocusReturned={onFocusReturned}
  /></QueryClientProvider>)
  await screen.findByRole('dialog', { name: task.title })
  view.unmount()

  trigger.dispatchEvent(new FocusEvent('blur'))
  expect(onFocusReturned).toHaveBeenCalledTimes(1)

  oldOwner.remove()
  const newRefresh = document.createElement('button')
  newRefresh.setAttribute('data-workspace-focus-fallback', '')
  document.body.appendChild(newRefresh)
  newRefresh.focus()

  await act(async () => { await Promise.resolve() })
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(newRefresh).toHaveFocus()
  newRefresh.remove()
})
