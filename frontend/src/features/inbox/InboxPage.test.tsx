import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, vi } from 'vitest'
import { InboxPage } from './InboxPage'
import { capture, workspace } from '../../test/fixtures'
import { verifiedMicrosoftProviderGates } from '../../test/providerGates'

const originalChrome = (window as Window & { chrome?: unknown }).chrome

afterEach(() => {
  Object.defineProperty(window, 'chrome', { configurable: true, value: originalChrome })
})

test('links a sanitized capture to the hinted task', async () => {
  const onLink = vi.fn().mockResolvedValue(undefined)
  const onCopyMicrosoftRequest = vi.fn()
  const onImportAgentResult = vi.fn()
  render(
    <InboxPage
      captures={[capture]}
      onCreateSourceTask={vi.fn()}
      onConvert={vi.fn()}
      onDismiss={vi.fn()}
      onCopyMicrosoftRequest={onCopyMicrosoftRequest}
      onImportAgentResult={onImportAgentResult}
      onImport={vi.fn()}
      onLink={onLink}
      onSearchChange={vi.fn()}
      onSelectCapture={vi.fn()}
      providerGates={verifiedMicrosoftProviderGates}
      search=""
      selectedCaptureId={null}
      workspace={workspace}
    />,
  )

  expect(screen.getByText('Release review feedback')).toBeInTheDocument()
  expect(screen.getByText('Manual import')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Copy Microsoft 365 request' }))
  await userEvent.click(screen.getByRole('button', { name: 'Import agent result' }))
  expect(onCopyMicrosoftRequest).toHaveBeenCalledOnce()
  expect(onImportAgentResult).toHaveBeenCalledOnce()
  await userEvent.click(screen.getByRole('button', { name: 'Link to task' }))
  expect(onLink).toHaveBeenCalledWith('C-0001', 'T-0001')
})

test('keeps unavailable agent handoff out of the primary UI while preserving manual import', () => {
  render(
    <InboxPage
      captures={[]}
      onCreateSourceTask={vi.fn()}
      onConvert={vi.fn()}
      onDismiss={vi.fn()}
      onCopyMicrosoftRequest={vi.fn()}
      onImportAgentResult={vi.fn()}
      onImport={vi.fn()}
      onLink={vi.fn()}
      onSearchChange={vi.fn()}
      onSelectCapture={vi.fn()}
      search=""
      selectedCaptureId={null}
      workspace={workspace}
    />,
  )

  expect(screen.queryByRole('button', { name: 'Copy Microsoft 365 request' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Import agent result' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Import packet' })).toBeEnabled()
  expect(screen.queryByText(/no Outlook or Teams read capability has passed Gate 0/i)).not.toBeInTheDocument()
})

test('captures the current embedded Microsoft URL without asking the user to paste it', async () => {
  const listeners = new Set<(event: MessageEvent) => void>()
  const postMessage = vi.fn((message: string) => {
    if (!message.startsWith('workstack-source-host|capture|')) return
    const requestId = message.split('|')[3]
    queueMicrotask(() => listeners.forEach((listener) => listener(new MessageEvent('message', { data: {
      type: 'workstack-source-draft',
      request_id: requestId,
      provider: 'outlook',
      url: 'https://outlook.office.com/mail/inbox/id/abc',
      title: 'Reliability review - Outlook',
      text: 'Review the reliability source\nTurn the selected message into an action.',
    } }))))
  })
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    postMessage,
    addEventListener: (_type: 'message', listener: (event: MessageEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: 'message', listener: (event: MessageEvent) => void) => listeners.delete(listener),
  } } })
  const onCreateSourceTask = vi.fn().mockResolvedValue(undefined)
  render(
    <InboxPage
      captures={[]}
      onConvert={vi.fn()}
      onCreateSourceTask={onCreateSourceTask}
      onDismiss={vi.fn()}
      onCopyMicrosoftRequest={vi.fn()}
      onImportAgentResult={vi.fn()}
      onImport={vi.fn()}
      onLink={vi.fn()}
      onSearchChange={vi.fn()}
      onSelectCapture={vi.fn()}
      search=""
      selectedCaptureId={null}
      workspace={workspace}
    />,
  )

  await userEvent.click(screen.getByRole('button', { name: 'Capture Outlook source' }))
  expect(await screen.findByText('Specific Outlook item link captured')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'https://outlook.office.com/mail/inbox/id/abc' })).toHaveAttribute(
    'href',
    'https://outlook.office.com/mail/inbox/id/abc',
  )
  expect(screen.queryByLabelText('Source URL')).not.toBeInTheDocument()
  expect(screen.queryByRole('group', { name: 'Source' })).not.toBeInTheDocument()
  expect(screen.getByLabelText('Capture title')).toHaveValue('Reliability review - Outlook')
  expect(screen.getByLabelText('Captured source content')).toHaveValue('Review the reliability source\nTurn the selected message into an action.')
  expect(screen.getByLabelText('Task title')).toHaveValue('')
  expect(screen.getByText('YYYY-MM-DD')).toBeInTheDocument()
  await userEvent.click(screen.getByLabelText('Task title'))
  await userEvent.keyboard('{ArrowRight}')
  expect(screen.getByLabelText('Task title')).toHaveValue('Reliability review - Outlook')
  expect(screen.getByLabelText('Action detail')).toHaveValue('Review the reliability source\nTurn the selected message into an action.')
  await userEvent.click(screen.getByRole('button', { name: 'Create Task from source' }))
  expect(onCreateSourceTask).toHaveBeenCalledWith(expect.objectContaining({
    sourceUrl: 'https://outlook.office.com/mail/inbox/id/abc',
    captureTitle: 'Reliability review - Outlook',
    taskTitle: 'Reliability review - Outlook',
    taskId: null,
  }))
})

test('attaches a reviewed embedded source to an existing Task without creating a second Task', async () => {
  const listeners = new Set<(event: MessageEvent) => void>()
  const postMessage = vi.fn((message: string) => {
    if (!message.startsWith('workstack-source-host|capture|')) return
    const requestId = message.split('|')[3]
    queueMicrotask(() => listeners.forEach((listener) => listener(new MessageEvent('message', { data: {
      type: 'workstack-source-draft',
      request_id: requestId,
      provider: 'outlook',
      url: 'https://outlook.live.com/mail/inbox/id/opaque',
      title: 'Reliability follow-up',
      text: 'Reliability follow-up\nAttach this evidence to the existing work.',
    } }))))
  })
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
    postMessage,
    addEventListener: (_type: 'message', listener: (event: MessageEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: 'message', listener: (event: MessageEvent) => void) => listeners.delete(listener),
  } } })
  const onCreateSourceTask = vi.fn().mockResolvedValue(undefined)
  render(
    <InboxPage
      captures={[]}
      onConvert={vi.fn()}
      onCreateSourceTask={onCreateSourceTask}
      onDismiss={vi.fn()}
      onCopyMicrosoftRequest={vi.fn()}
      onImportAgentResult={vi.fn()}
      onImport={vi.fn()}
      onLink={vi.fn()}
      onSearchChange={vi.fn()}
      onSelectCapture={vi.fn()}
      search=""
      selectedCaptureId={null}
      workspace={workspace}
    />,
  )

  await userEvent.click(screen.getByRole('button', { name: 'Capture Outlook source' }))
  await screen.findByText('Specific Outlook item link captured')
  await userEvent.click(screen.getByRole('radio', { name: /Attach to existing Task/i }))
  await userEvent.selectOptions(screen.getByLabelText('Existing Task'), 'T-0001')
  expect(screen.getByLabelText('Capture title')).toHaveValue('Reliability follow-up')
  expect(screen.getByLabelText('Captured source content')).toHaveValue('Reliability follow-up\nAttach this evidence to the existing work.')
  await userEvent.click(screen.getByRole('button', { name: 'Attach to selected Task' }))

  expect(onCreateSourceTask).toHaveBeenCalledWith(expect.objectContaining({
    sourceUrl: 'https://outlook.live.com/mail/inbox/id/opaque',
    taskId: 'T-0001',
  }))
})

test('opens a reviewed Teams capture draft without claiming OOB provenance', async () => {
  const onCreateSourceTask = vi.fn().mockResolvedValue(undefined)
  render(
    <InboxPage
      captures={[]}
      onConvert={vi.fn()}
      onCreateSourceTask={onCreateSourceTask}
      onDismiss={vi.fn()}
      onCopyMicrosoftRequest={vi.fn()}
      onImportAgentResult={vi.fn()}
      onImport={vi.fn()}
      onLink={vi.fn()}
      onSearchChange={vi.fn()}
      onSelectCapture={vi.fn()}
      search=""
      selectedCaptureId={null}
      workspace={workspace}
    />,
  )

  await userEvent.click(screen.getByRole('button', { name: 'Capture copied Teams content' }))
  await userEvent.type(screen.getByLabelText('Capture title'), 'Open the Webex room')
  await userEvent.type(screen.getByLabelText('Captured source content'), 'Open the meeting room before the review.')
  await userEvent.click(screen.getByLabelText('Task title'))
  await userEvent.keyboard('{Tab}')
  await userEvent.type(screen.getByLabelText('Action detail'), 'Open the meeting room before the review.')
  await userEvent.click(screen.getByRole('button', { name: 'Create Task from source' }))

  expect(onCreateSourceTask).toHaveBeenCalledWith(expect.objectContaining({
    provider: 'teams',
    captureTitle: 'Open the Webex room',
    taskTitle: 'Open the Webex room',
    text: 'Open the meeting room before the review.',
    priority: 'P2',
  }))
})

test('keeps one capture identity across an explicit retry', async () => {
  const onCreateSourceTask = vi.fn()
    .mockRejectedValueOnce(new Error('Temporary transport failure'))
    .mockResolvedValueOnce(undefined)
  render(
    <InboxPage
      captures={[]}
      onConvert={vi.fn()}
      onCreateSourceTask={onCreateSourceTask}
      onDismiss={vi.fn()}
      onCopyMicrosoftRequest={vi.fn()}
      onImportAgentResult={vi.fn()}
      onImport={vi.fn()}
      onLink={vi.fn()}
      onSearchChange={vi.fn()}
      onSelectCapture={vi.fn()}
      search=""
      selectedCaptureId={null}
      workspace={workspace}
    />,
  )

  await userEvent.click(screen.getByRole('button', { name: 'Capture copied OneNote content' }))
  await userEvent.type(screen.getByLabelText('Capture title'), 'Publish the reviewed note')
  await userEvent.type(screen.getByLabelText('Captured source content'), 'Keep the same capture identity when retrying.')
  await userEvent.click(screen.getByLabelText('Task title'))
  await userEvent.keyboard('{ArrowRight}')
  await userEvent.type(screen.getByLabelText('Action detail'), 'Keep the same capture identity when retrying.')
  await userEvent.click(screen.getByRole('button', { name: 'Create Task from source' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Temporary transport failure')
  await userEvent.click(screen.getByRole('button', { name: 'Create Task from source' }))

  expect(onCreateSourceTask).toHaveBeenCalledTimes(2)
  expect(onCreateSourceTask.mock.calls[1][0].capturedAt).toBe(onCreateSourceTask.mock.calls[0][0].capturedAt)
})

test('keeps Microsoft navigation inside Source Inbox when the desktop host is available', async () => {
  const postMessage = vi.fn()
  Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: { postMessage } } })
  const rendered = render(
    <InboxPage
      captures={[]}
      onConvert={vi.fn()}
      onCreateSourceTask={vi.fn()}
      onDismiss={vi.fn()}
      onCopyMicrosoftRequest={vi.fn()}
      onImportAgentResult={vi.fn()}
      onImport={vi.fn()}
      onLink={vi.fn()}
      onSearchChange={vi.fn()}
      onSelectCapture={vi.fn()}
      search=""
      selectedCaptureId={null}
      workspace={workspace}
    />,
  )

  expect(screen.queryByRole('link', { name: 'Open web app' })).not.toBeInTheDocument()
  expect(screen.getByRole('tabpanel', { name: 'Outlook web app' })).toBeInTheDocument()
  expect(postMessage).toHaveBeenCalledWith(expect.stringMatching(/^workstack-source-host\|show\|outlook\|/))

  await userEvent.click(screen.getByRole('tab', { name: /Teams/ }))
  expect(screen.getByRole('tabpanel', { name: 'Teams web app' })).toBeInTheDocument()
  expect(postMessage).toHaveBeenCalledWith(expect.stringMatching(/^workstack-source-host\|show\|teams\|/))

  rendered.unmount()
  expect(postMessage).toHaveBeenLastCalledWith('workstack-source-host|hide')
})
