import { StrictMode, type PropsWithChildren } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'

import { api } from '../../api/client'
import type { CheckpointAudit, CheckpointAuditEntry } from '../../domain/types'
import { useTaskResumeFacts } from './useTaskResumeFacts'

/**
 * The hook owns one thing beyond the selector: the read. So these cases are
 * about the read — that two panels on one Task join a single request under the
 * key Daily Review already uses, that a Task or workspace change re-binds in the
 * same pass instead of leaving the previous Task's plan on screen, and that a
 * failure is a stated failure with an explicit retry rather than a silent empty
 * state.
 */

const WORKSPACE = '123e4567-e89b-42d3-a456-426614174000'
const OTHER_WORKSPACE = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const TASK = 'T-0033'
const OTHER_TASK = 'T-0041'

function entry(taskId: string, next: string, seed: string, workspaceUid = WORKSPACE): CheckpointAuditEntry {
  const id = `CP-${seed.repeat(64).slice(0, 64)}`
  const entryDigest = `sha256:${seed.repeat(64).slice(0, 64)}`
  const locator = { workspace_uid: workspaceUid, task_id: taskId, date: '2026-09-06', ordinal: 0, entry_digest: entryDigest }
  return {
    locator,
    checkpoint_id: id,
    entry: { task_id: taskId, next: [next] },
    recorded: {
      type: 'worklog.recorded',
      workspace_uid: workspaceUid,
      task_id: taskId,
      checkpoint_id: id,
      date: locator.date,
      ordinal: 0,
      entry_digest: entryDigest,
      origin: 'agent-cli-v1',
    },
    state: 'active',
    revision: 0,
    transitions: [],
  }
}

const AUDIT: CheckpointAudit = {
  workspace_uid: WORKSPACE,
  entries: [entry(TASK, 'ship the resume view', 'a'), entry(OTHER_TASK, 'write the report', 'b')],
}
const OTHER_AUDIT: CheckpointAudit = {
  workspace_uid: OTHER_WORKSPACE,
  entries: [entry(TASK, 'a different workspace plan', 'c', OTHER_WORKSPACE)],
}

interface Binding { workspaceUid: string; taskId: string }

function harness() {
  const read = vi.spyOn(api, 'getCheckpointAudit').mockImplementation(() => Promise.resolve(AUDIT))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: PropsWithChildren) => (
    <StrictMode><QueryClientProvider client={client}>{children}</QueryClientProvider></StrictMode>
  )
  return { client, read, wrapper }
}

function setup(initialProps: Binding = { workspaceUid: WORKSPACE, taskId: TASK }) {
  const rig = harness()
  const hook = renderHook(
    ({ workspaceUid, taskId }: Binding) => useTaskResumeFacts(workspaceUid, taskId),
    { wrapper: rig.wrapper, initialProps },
  )
  return { ...hook, ...rig }
}

test('reads the shared workspace audit and reports the Task it was asked for', async () => {
  const view = setup()
  expect(view.result.current.status).toBe('loading')
  await waitFor(() => expect(view.result.current.status).toBe('ready'))
  expect(view.result.current.next).toEqual(['ship the resume view'])
  expect(view.result.current.taskId).toBe(TASK)
  expect(view.client.getQueryData(['checkpoint-audit', WORKSPACE])).toEqual(AUDIT)
})

test('two panels on the same Task share one request and one set of records', async () => {
  const rig = harness()
  // The drawer and the handoff panel both mount this hook. They read the key
  // Daily Review already uses, so they join one request rather than opening a
  // second view of the same records.
  const both = renderHook(
    () => [useTaskResumeFacts(WORKSPACE, TASK), useTaskResumeFacts(WORKSPACE, TASK)] as const,
    { wrapper: rig.wrapper },
  )
  await waitFor(() => expect(both.result.current[0].status).toBe('ready'))
  expect(both.result.current[1].version).toBe(both.result.current[0].version)
  expect(rig.read).toHaveBeenCalledTimes(1)
})

test('navigating to another Task re-binds in the same pass', async () => {
  const view = setup()
  await waitFor(() => expect(view.result.current.next).toEqual(['ship the resume view']))
  view.rerender({ workspaceUid: WORKSPACE, taskId: OTHER_TASK })
  // No intermediate render carries T-0033's plan under T-0041's name.
  expect(view.result.current.taskId).toBe(OTHER_TASK)
  expect(view.result.current.next).toEqual(['write the report'])
})

test('a workspace change reads the new workspace and never serves the old one', async () => {
  const view = setup()
  await waitFor(() => expect(view.result.current.status).toBe('ready'))
  view.read.mockImplementation(() => Promise.resolve(OTHER_AUDIT))
  view.rerender({ workspaceUid: OTHER_WORKSPACE, taskId: TASK })
  expect(view.result.current.status).toBe('loading')
  expect(view.result.current.next).toEqual([])
  await waitFor(() => expect(view.result.current.next).toEqual(['a different workspace plan']))
  expect(view.result.current.workspaceUid).toBe(OTHER_WORKSPACE)
})

test('no selected Task starts no read and states an empty snapshot', () => {
  const view = setup({ workspaceUid: WORKSPACE, taskId: '' })
  expect(view.result.current.status).toBe('empty')
  expect(view.read).not.toHaveBeenCalled()
})

test('a failed read is stated, and only an explicit retry re-reads it', async () => {
  const view = setup()
  view.read.mockImplementation(() => Promise.reject(new Error('checkpoint audit unavailable')))
  view.rerender({ workspaceUid: OTHER_WORKSPACE, taskId: TASK })
  await waitFor(() => expect(view.result.current.status).toBe('error'))
  expect(view.result.current.errorMessage).toBe('checkpoint audit unavailable')
  expect(view.result.current.provenance).toBeNull()

  const attempts = view.read.mock.calls.length
  view.read.mockImplementation(() => Promise.resolve(OTHER_AUDIT))
  await waitFor(() => expect(view.read.mock.calls.length).toBe(attempts))
  view.result.current.retry()
  await waitFor(() => expect(view.result.current.status).toBe('ready'))
  expect(view.result.current.next).toEqual(['a different workspace plan'])
})
