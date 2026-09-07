import { useCallback } from 'react'

import { copyTextToClipboard } from '../../utils/clipboard'
import { deleteErrorMessage, saveErrorMessage } from './reportDraftMessages'
import type { DraftSession } from './useReportDraftSession'
import type { ReportDraftCoordinate, ReportDraftStore } from './reportDraftStorage'

/**
 * The four things the reader can ask for, each written so a refusal costs them
 * nothing: the text on screen is never rewritten by a failed save or delete, and
 * both fallbacks act on exactly what is in the editor now.
 */

const PROMPT_MOVED_ON =
  'The saved draft changed while this prompt was open, so nothing was deleted. Check the current draft and try again.'

function downloadMarkdown(coordinate: ReportDraftCoordinate, markdown: string) {
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `workstack-daily-${coordinate.date}-draft.md`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export function useSaveDraft(session: DraftSession, store: ReportDraftStore, blocked: boolean) {
  const { dispatch, guard, pinned, state } = session
  return useCallback(() => {
    if (state.base === null || blocked) return
    const token = guard.owner.current
    const pendingText = state.text
    dispatch({ type: 'begin' })
    void store
      .save({ ...pinned, ...state.base, markdown: pendingText }, state.localRevision)
      .then(
        guard.owned(token, (result: Awaited<ReturnType<ReportDraftStore['save']>>) => {
          if (!result.ok) {
            // The input is untouched on purpose: a refused write must never cost
            // the reader the text they were trying to keep.
            return dispatch({ type: 'failed', notice: saveErrorMessage(result.code) })
          }
          // The baseline becomes the SNAPSHOT that was written, so anything typed
          // while the write was in flight is still unsaved.
          return dispatch({
            type: 'saved',
            localRevision: result.value.localRevision,
            markdown: result.value.markdown,
          })
        }),
        guard.owned(token, () =>
          dispatch({ type: 'failed', notice: saveErrorMessage('storage_unavailable') }),
        ),
      )
  }, [blocked, dispatch, guard, pinned, state.base, state.localRevision, state.text, store])
}

export function useDeleteDraft(session: DraftSession, store: ReportDraftStore) {
  const { dispatch, guard, pinned, state } = session
  return useCallback(
    (promptedRevision: number) => {
      dispatch({ type: 'prompt', confirmation: null })
      if (state.busy) return
      if (state.localRevision === null || state.localRevision !== promptedRevision) {
        // The saved draft moved on behind the prompt. Confirming here would
        // delete a revision the reader never saw, so it refuses instead.
        return dispatch({ type: 'refused', notice: PROMPT_MOVED_ON })
      }
      const token = guard.owner.current
      dispatch({ type: 'begin' })
      return void store.remove(pinned, promptedRevision).then(
        guard.owned(token, (result: Awaited<ReturnType<ReportDraftStore['remove']>>) => {
          if (!result.ok) {
            return dispatch({ type: 'failed', notice: deleteErrorMessage(result.code) })
          }
          return dispatch({ type: 'deleted' })
        }),
        guard.owned(token, () =>
          dispatch({ type: 'failed', notice: deleteErrorMessage('storage_unavailable') }),
        ),
      )
    },
    [dispatch, guard, pinned, state.busy, state.localRevision, store],
  )
}

export function useKeepFallbacks(session: DraftSession, prompting: boolean) {
  const { dispatch, guard, pinned, state } = session
  const copy = useCallback(() => {
    if (prompting) return
    const token = guard.owner.current
    void copyTextToClipboard(state.text).then(
      guard.owned(token, () =>
        dispatch({ type: 'note', status: 'The current text was copied as Markdown.' }),
      ),
      guard.owned(token, () =>
        dispatch({
          type: 'failed',
          notice: 'The clipboard is unavailable in this browser. Use Download .md instead.',
        }),
      ),
    )
  }, [dispatch, guard, prompting, state.text])

  const download = useCallback(() => {
    if (prompting) return
    try {
      downloadMarkdown(pinned, state.text)
      dispatch({ type: 'note', status: 'The current text was downloaded as a .md file.' })
    } catch {
      dispatch({
        type: 'failed',
        notice: 'The file could not be downloaded in this browser. Use Copy Markdown instead.',
      })
    }
  }, [dispatch, pinned, prompting, state.text])

  return { copy, download }
}
