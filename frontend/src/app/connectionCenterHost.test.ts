import { expect, test } from 'vitest'

import {
  type EditorContext,
  type PendingRequest,
  fingerprint,
  isStaleForEditor,
  nextEditorGeneration,
  presentHostError,
  releaseRecovery,
} from './connectionCenterHost'

const workspaceId = '11111111-1111-4111-8111-111111111111'
const draft = {
  profile_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  label: 'Local planning',
  kind: 'local' as const,
  enabled: true,
  live_updates: true,
  expected_workspace_id: workspaceId,
  data_dir: 'C:/WorkStack/planning-ssot',
}

function pending(overrides: Partial<PendingRequest> = {}): PendingRequest {
  return {
    requestId: 'req-1',
    owner: 1,
    generation: 1,
    purpose: 'editor',
    fingerprint: fingerprint(draft),
    timeoutId: 0,
    ...overrides,
  }
}

function editorAt(generation: number, currentDraft = draft): Pick<EditorContext, 'generationRef' | 'draftRef' | 'refreshRef'> {
  return {
    generationRef: { current: generation },
    draftRef: { current: currentDraft },
    refreshRef: { current: null },
  }
}

test('an A to B to A fingerprint is still stale when the editor generation moved', () => {
  const issued = pending({ generation: 1, fingerprint: fingerprint(draft) })
  // The draft bytes returned to A, but the request still belongs to generation 1.
  expect(isStaleForEditor(issued, editorAt(3) as EditorContext)).toBe(true)
  expect(isStaleForEditor(issued, editorAt(1) as EditorContext)).toBe(false)
})

test('fingerprint mismatch is an additional exact-candidate fence at the same generation', () => {
  const issued = pending({ generation: 2, fingerprint: fingerprint(draft) })
  const edited = { ...draft, label: 'Temporary edit' }
  expect(isStaleForEditor(issued, editorAt(2, edited) as EditorContext)).toBe(true)
})

test('registry-recovery and ambient replies are not fenced by editor generation', () => {
  const recovery = pending({ purpose: 'registry-recovery', generation: 1 })
  const ambient = pending({ purpose: 'ambient', generation: 1, fingerprint: undefined })
  expect(isStaleForEditor(recovery, editorAt(9) as EditorContext)).toBe(false)
  expect(isStaleForEditor(ambient, editorAt(9) as EditorContext)).toBe(false)
})

test('nextEditorGeneration advances identity without inspecting draft bytes', () => {
  const editor = editorAt(4) as EditorContext
  nextEditorGeneration(editor)
  expect(editor.generationRef.current).toBe(5)
})

test('presentHostError maps known codes and allowlists pid without leaking unknown details', () => {
  expect(presentHostError('activation_conflict', { pid: 42 })).toEqual({
    code: 'activation_conflict',
    message: 'Another unconfirmed activation targets this connection state with an unknown outcome. This activation was refused and nothing was written.',
    pid: 42,
  })
  expect(presentHostError('not-a-real-code', { pid: 0 })).toEqual({
    code: 'ssh_test_failed',
    message: 'The SSH profile could not be verified.',
  })
})

test('releaseRecovery retires only the exact armed request id', () => {
  const editor = editorAt(1) as EditorContext
  editor.refreshRef.current = 'reload-1'
  releaseRecovery(editor, 'reload-2')
  expect(editor.refreshRef.current).toBe('reload-1')
  releaseRecovery(editor, 'reload-1')
  expect(editor.refreshRef.current).toBeNull()
})
