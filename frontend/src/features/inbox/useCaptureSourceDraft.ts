import { useEffect, useRef, useState } from 'react'
import type { Capture } from '../../domain/types'

export interface CaptureSourceConflict {
  from: number
  to: number
}

export interface CaptureSourceDraft {
  title: string
  detail: string
  tags: string
  sourceConflict: CaptureSourceConflict | null
  refreshedRevision: number | null
  editTitle: (value: string) => void
  editDetail: (value: string) => void
  editTags: (value: string) => void
  keepDraft: () => void
  refreshSourceFields: () => void
}

interface LoadedSource {
  id: string
  revision: number
  fingerprint: string
}

function loadedSourceOf(capture: Capture): LoadedSource {
  return { id: capture.id, revision: capture.revision, fingerprint: capture.source.fingerprint }
}

function sourceDetailOf(capture: Capture) {
  return capture.normalized.context || capture.normalized.summary
}

/**
 * The title, context and tags a Capture prefills, plus the policy deciding when
 * a newer sanitized revision may overwrite an edited draft: adopt silently,
 * refresh, or hold the draft and report a conflict. `onCaptureReplaced` lets the
 * drawer reset the task-only fields it still owns.
 */
export function useCaptureSourceDraft(
  capture: Capture,
  formOpen: boolean,
  onCaptureReplaced: () => void,
): CaptureSourceDraft {
  const loadedSource = useRef(loadedSourceOf(capture))
  const [title, setTitle] = useState(capture.source.display_title)
  const [detail, setDetail] = useState(sourceDetailOf(capture))
  const [tags, setTags] = useState(capture.normalized.tags.join(', '))
  const [sourceDirty, setSourceDirty] = useState(false)
  const [sourceConflict, setSourceConflict] = useState<CaptureSourceConflict | null>(null)
  const [refreshedRevision, setRefreshedRevision] = useState<number | null>(null)
  const captureReplaced = useRef(onCaptureReplaced)
  captureReplaced.current = onCaptureReplaced

  const refreshSourceFields = () => {
    setTitle(capture.source.display_title)
    setDetail(sourceDetailOf(capture))
    setTags(capture.normalized.tags.join(', '))
    setSourceDirty(false)
    setSourceConflict(null)
    setRefreshedRevision(capture.revision)
    loadedSource.current = loadedSourceOf(capture)
  }

  useEffect(() => {
    const loaded = loadedSource.current
    if (capture.id !== loaded.id) {
      loadedSource.current = loadedSourceOf(capture)
      setTitle(capture.source.display_title)
      setDetail(sourceDetailOf(capture))
      setTags(capture.normalized.tags.join(', '))
      setSourceDirty(false)
      setSourceConflict(null)
      setRefreshedRevision(null)
      captureReplaced.current()
      return
    }
    if (capture.revision <= loaded.revision) return
    if (capture.source.fingerprint === loaded.fingerprint) {
      loadedSource.current = { ...loaded, revision: capture.revision }
      return
    }
    if (formOpen && sourceDirty) {
      setSourceConflict({ from: loaded.revision, to: capture.revision })
      setRefreshedRevision(null)
      return
    }
    refreshSourceFields()
  }, [capture.id, capture.revision, capture.source.fingerprint, formOpen, sourceDirty])

  const keepDraft = () => {
    loadedSource.current = loadedSourceOf(capture)
    setSourceConflict(null)
    setRefreshedRevision(null)
  }

  const edit = (apply: (value: string) => void) => (value: string) => {
    setSourceDirty(true)
    setRefreshedRevision(null)
    apply(value)
  }

  return {
    title,
    detail,
    tags,
    sourceConflict,
    refreshedRevision,
    editTitle: edit(setTitle),
    editDetail: edit(setDetail),
    editTags: edit(setTags),
    keepDraft,
    refreshSourceFields,
  }
}
