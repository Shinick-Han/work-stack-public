import { useCallback, useMemo } from 'react'

import {
  browserReportDraftStorage,
  createReportDraftStore,
  runWithBrowserExclusiveLock,
  type ReportDraftCoordinate,
  type ReportDraftStore,
} from './reportDraftStorage'
import type { Confirmation, DailyReportDraftSource, DraftSessionState } from './reportDraftEditorModel'
import { useDeleteDraft, useKeepFallbacks, useSaveDraft } from './useReportDraftCommands'
import { useReportDraftSession } from './useReportDraftSession'

/**
 * The editor's view model: one session, the four commands, and the prompt rules.
 *
 * Composition only. The session and its ownership guard live in
 * `useReportDraftSession`, the storage-facing commands in
 * `useReportDraftCommands`, and the pure state machine in
 * `reportDraftEditorModel`.
 */

export interface ReportDraftEditorInput {
  coordinate: ReportDraftCoordinate
  source: DailyReportDraftSource
  onClose: () => void
  store?: ReportDraftStore
}

export interface ReportDraftEditorModel {
  actions: {
    cancelPrompt: () => void
    confirmDelete: (revision: number) => void
    confirmDiscard: () => void
    copy: () => void
    download: () => void
    promptDelete: () => void
    requestClose: () => void
    save: () => void
    setText: (value: string) => void
  }
  dirty: boolean
  pinned: ReportDraftCoordinate
  prompting: boolean
  state: DraftSessionState
}

function defaultStore(): ReportDraftStore {
  return createReportDraftStore({
    storage: browserReportDraftStorage,
    withExclusiveLock: runWithBrowserExclusiveLock,
  })
}

export function useReportDraftEditor({
  coordinate,
  onClose,
  source,
  store,
}: ReportDraftEditorInput): ReportDraftEditorModel {
  const resolvedStore = useMemo(() => store ?? defaultStore(), [store])
  const session = useReportDraftSession({ coordinate, source, store: resolvedStore })
  const { dirty, dispatch, pinned, state } = session
  const prompting = state.confirmation !== null
  const blocked = state.busy || prompting

  const save = useSaveDraft(session, resolvedStore, blocked)
  const confirmDelete = useDeleteDraft(session, resolvedStore)
  const { copy, download } = useKeepFallbacks(session, prompting)

  const prompt = useCallback(
    (confirmation: Confirmation) => dispatch({ type: 'prompt', confirmation }),
    [dispatch],
  )

  const requestClose = useCallback(() => {
    // A prompt owns the interaction while it is open, so the outer dialog's own
    // close paths — Escape, backdrop and the header button — do not slip past it.
    if (blocked) return
    if (dirty) return prompt({ kind: 'discard' })
    return onClose()
  }, [blocked, dirty, onClose, prompt])

  const actions = useMemo(
    () => ({
      cancelPrompt: () => prompt(null),
      confirmDelete,
      confirmDiscard: () => {
        prompt(null)
        onClose()
      },
      copy,
      download,
      promptDelete: () => {
        if (state.localRevision === null) return
        prompt({ kind: 'delete', revision: state.localRevision })
      },
      requestClose,
      save,
      setText: (value: string) => dispatch({ type: 'edit', text: value }),
    }),
    [confirmDelete, copy, dispatch, download, onClose, prompt, requestClose, save, state.localRevision],
  )

  return { actions, dirty, pinned, prompting, state }
}
