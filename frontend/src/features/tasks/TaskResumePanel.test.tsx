import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { task, workspace } from '../../test/fixtures'
import { TaskResumeFooter, TaskResumePanel } from './TaskResumePanel'
import type { TaskResumeFactsResult } from './useTaskResumeFacts'

const emptyFacts = (): TaskResumeFactsResult => ({
  status: 'empty',
  workspaceUid: workspace.workspace.id,
  taskId: task.id,
  provenance: null,
  done: [],
  next: [],
  blockers: [],
  extras: [],
  unreadableReason: null,
  errorMessage: null,
  activeRecordCount: 0,
  supersededRecordCount: 0,
  version: `v1:empty:${workspace.workspace.id}:${task.id}`,
  retry: vi.fn(),
})

const savedTask = {
  id: task.id,
  uid: task.uid,
  revision: task.revision,
  title: task.title,
  detail: task.detail,
  due: task.due,
  priority: task.priority,
  scheduled: task.scheduled,
}

function renderResume(onOpenContext = vi.fn(), facts: TaskResumeFactsResult = emptyFacts()) {
  return render(
    <TaskResumePanel
      contextCount={2}
      facts={facts}
      objectives={workspace.objectives}
      onOpenContext={onOpenContext}
      onOpenObjective={vi.fn()}
      progressLocked={false}
      savedTask={savedTask}
      workspaceUid={workspace.workspace.id}
    />,
  )
}

test('resume shows empty progress copy before identity and opens the context subview from both entries', async () => {
  const onOpenContext = vi.fn()
  renderResume(onOpenContext)
  expect(screen.getByRole('heading', { name: task.title })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Next step' })).toBeInTheDocument()
  expect(screen.getByText('No progress recorded yet.')).toBeInTheDocument()
  expect(screen.queryByRole('group', { name: 'Task identity' })).not.toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'References for this task' })).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Prepare resume brief' }))
  await userEvent.click(screen.getByRole('button', { name: 'View all context (2)' }))
  expect(onOpenContext).toHaveBeenCalledTimes(2)
  expect(screen.getByText('Copy this brief into your agent session. Nothing is sent automatically.')).toBeInTheDocument()
})

test('resume renders the latest next and blockers before task details and expands long titles in place', async () => {
  const facts = emptyFacts()
  facts.status = 'ready'
  facts.next = ['Keep the original 제목 in the brief']
  facts.blockers = ['Waiting on UI-B']
  facts.provenance = {
    workspaceUid: workspace.workspace.id,
    taskId: task.id,
    checkpointId: 'CP-9',
    entryDigest: 'd1',
    date: '2026-09-07',
    ordinal: 1,
    revision: 0,
    origin: null,
    binding: 'locator',
    recordedTaskId: task.id,
    recordedTaskTitle: task.title,
  }
  const longTitle = `${task.title} ${'and-then-a-much-longer-continuation-that-must-remain-intact '.repeat(4)}`.trim()
  render(
    <TaskResumePanel
      contextCount={0}
      facts={facts}
      objectives={[]}
      onOpenContext={vi.fn()}
      progressLocked={false}
      savedTask={{ ...savedTask, title: longTitle }}
      workspaceUid={workspace.workspace.id}
    />,
  )
  expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(longTitle)
  expect(screen.getByText('Keep the original 제목 in the brief')).toBeInTheDocument()
  expect(screen.getByText('Waiting on UI-B')).toBeInTheDocument()
  expect(screen.getByText('From the latest checkpoint · 2026-09-07')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Show full title' }))
  expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true')
  await userEvent.click(screen.getByText('Task details'))
  expect(screen.getByText(task.uid)).toBeInTheDocument()
  expect(screen.getByText(`Revision ${task.revision}`)).toBeInTheDocument()
})

test('unreadable latest records stay explicit and expose extras in history', async () => {
  const facts = emptyFacts()
  facts.status = 'unreadable'
  facts.unreadableReason = 'No readable summary for this entry'
  facts.extras = [{ label: 'raw', value: '{"broken":true}' }]
  facts.activeRecordCount = 1
  facts.supersededRecordCount = 2
  facts.provenance = {
    workspaceUid: workspace.workspace.id,
    taskId: task.id,
    checkpointId: 'CP-bad',
    entryDigest: null,
    date: '2026-09-08',
    ordinal: 3,
    revision: 1,
    origin: null,
    binding: 'locator',
    recordedTaskId: task.id,
    recordedTaskTitle: null,
  }
  renderResume(vi.fn(), facts)
  expect(screen.getAllByRole('alert')[0]).toHaveTextContent('No readable summary for this entry')
  await userEvent.click(screen.getByText('Record details and history'))
  expect(screen.getByText(/CP-bad/)).toBeInTheDocument()
  expect(screen.getByText(/2 superseded/)).toBeInTheDocument()
  expect(screen.getByText(/raw: \{"broken":true\}/)).toBeInTheDocument()
})

test('Record progress stays disabled while the Task save contract is locked', async () => {
  const onRecordProgress = vi.fn()
  const { rerender } = render(<TaskResumeFooter locked={false} onRecordProgress={onRecordProgress} taskId={task.id} />)
  await userEvent.click(screen.getByRole('button', { name: 'Record progress' }))
  expect(onRecordProgress).toHaveBeenCalledWith(task.id)
  rerender(<TaskResumeFooter locked onRecordProgress={onRecordProgress} taskId={task.id} />)
  expect(screen.getByRole('button', { name: 'Record progress' })).toBeDisabled()
  rerender(<TaskResumeFooter locked={false} taskId={task.id} />)
  expect(screen.getByRole('button', { name: 'Record progress' })).toBeDisabled()
})

test('an honest empty facts snapshot is not replaced with an unavailable placeholder', () => {
  renderResume()
  expect(screen.getByText('No progress recorded yet.')).toBeInTheDocument()
  expect(screen.queryByText('Recorded progress was not available in this view.')).not.toBeInTheDocument()
})

test('a partly readable record is not dressed up as an ordinary one', async () => {
  const facts = emptyFacts()
  facts.status = 'partial'
  facts.next = ['DISPUTED next step']
  facts.done = ['Kept the Korean 제목']
  facts.extras = [{ label: 'hand_off', value: '{"note":"unrecognised"}' }]
  facts.activeRecordCount = 1
  facts.provenance = {
    workspaceUid: workspace.workspace.id,
    taskId: task.id,
    checkpointId: 'CP-7',
    entryDigest: 'd7',
    date: '2026-09-07',
    ordinal: 1,
    revision: 0,
    origin: 'agent-cli-v1',
    binding: 'locator',
    recordedTaskId: 'T-9999',
    recordedTaskTitle: 'Some other Task',
  }
  renderResume(vi.fn(), facts)
  const notice = screen.getByRole('status')
  expect(notice).toHaveTextContent('This record could not be presented in full.')
  expect(notice).toHaveTextContent('The recorded entry names Task T-9999 · Some other Task, not T-0001.')
  expect(notice).toHaveTextContent('Kept under Record details and history, not shown above: hand_off.')
  expect(notice).toHaveTextContent('The recorded text below is shown exactly as it was saved.')
  // The recorded text itself is preserved, not withheld behind the notice.
  expect(screen.getByText('DISPUTED next step')).toBeInTheDocument()
  expect(screen.getByText('Kept the Korean 제목')).toBeInTheDocument()
  await userEvent.click(screen.getByText('Record details and history'))
  expect(screen.getByText(/hand_off: \{"note":"unrecognised"\}/)).toBeInTheDocument()
})

test('a fully readable record carries no partial notice', () => {
  const facts = emptyFacts()
  facts.status = 'ready'
  facts.next = ['Wire Review']
  renderResume(vi.fn(), facts)
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
  expect(screen.queryByText('This record could not be presented in full.')).not.toBeInTheDocument()
})

test('Recent progress states itself rather than heading an empty section', () => {
  const loading = emptyFacts()
  loading.status = 'loading'
  const { unmount } = renderResume(vi.fn(), loading)
  expect(screen.queryByRole('heading', { name: 'Recent progress' })).not.toBeInTheDocument()
  unmount()

  const unreadable = emptyFacts()
  unreadable.status = 'unreadable'
  unreadable.unreadableReason = 'No readable summary for this entry'
  unreadable.activeRecordCount = 1
  unreadable.supersededRecordCount = 1
  renderResume(vi.fn(), unreadable)
  const section = screen.getByRole('heading', { name: 'Recent progress' }).closest('section') as HTMLElement
  expect(within(section).getByText('Recent progress could not be read from this checkpoint.')).toBeInTheDocument()
  expect(within(section).getByText('Record details and history')).toBeInTheDocument()
})
