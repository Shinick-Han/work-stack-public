import { renderHook, act } from '@testing-library/react'
import { useState } from 'react'
import { expect, test } from 'vitest'
import { knowledgeImportEnvelope } from './knowledgeCaptureFixture'
import { useKnowledgeReviewHandoff } from './useKnowledgeReviewHandoff'

function useHarness(workspaceUid: string) {
  const [importOpen, setImportOpen] = useState(false)
  const review = useKnowledgeReviewHandoff({
    importOpen,
    importPending: false,
    setImportOpen,
    workspaceUid,
  })
  return { importOpen, review }
}

test('rejects a new handoff while import retry is frozen and clears on workspace change', () => {
  const envelope = knowledgeImportEnvelope()
  const other = knowledgeImportEnvelope({ request_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc' })
  const { result, rerender } = renderHook(
    ({ workspaceUid }: { workspaceUid: string }) => useHarness(workspaceUid),
    { initialProps: { workspaceUid: '22222222-2222-2222-2222-222222222222' } },
  )

  act(() => { result.current.review.onReviewKnowledge(envelope) })
  expect(result.current.importOpen).toBe(true)
  expect(result.current.review.prefill?.request_id).toBe(envelope.request_id)

  act(() => { result.current.review.onKnowledgeStatusChange({ pending: false, retrySame: true, canSubmit: true, blockedNewSearch: false }) })
  act(() => { result.current.review.onReviewKnowledge(other) })
  expect(result.current.review.prefill?.request_id).toBe(envelope.request_id)

  rerender({ workspaceUid: '33333333-3333-4333-8333-333333333333' })
  expect(result.current.review.prefill).toBeNull()
  expect(result.current.importOpen).toBe(true)
})

test('closes a non-busy execute handoff when the workspace changes', () => {
  const envelope = knowledgeImportEnvelope()
  const { result, rerender } = renderHook(
    ({ workspaceUid }: { workspaceUid: string }) => useHarness(workspaceUid),
    { initialProps: { workspaceUid: '22222222-2222-2222-2222-222222222222' } },
  )

  act(() => { result.current.review.onReviewKnowledge(envelope) })
  expect(result.current.importOpen).toBe(true)
  rerender({ workspaceUid: '33333333-3333-4333-8333-333333333333' })
  expect(result.current.review.prefill).toBeNull()
  expect(result.current.importOpen).toBe(false)
})
