import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Button, ErrorState, LoadingBlock } from '../../components/Primitives'
import type {
  DailyReportContextCatalog,
  DailyReportPreviewDocument,
  DailyReportPreviewResponse,
} from '../../domain/reporting'
import type { ReviewProjection, WorkspaceProjection } from '../../domain/types'
import { formatDate, formatDateTime, getErrorMessage } from '../../utils/format'
import { copyTextToClipboard } from '../../utils/clipboard'
import { DailyReportContextPanel } from './DailyReportContextPanel'
import { DailyReportDocument } from './DailyReportDocument'
import { DailyReportDraftEditor } from './DailyReportDraftEditor'
import type {
  DailyReportDraftEditorProps,
  DailyReportDraftSource,
} from './DailyReportDraftEditor'
import './dailyReportPreview.css'

interface DailyReportPreviewProps {
  date: string
  workspaceId: string
  sourceUpdatedAt: number | string
  /** When false, the generate panel is gated; an open editor stays mounted. */
  sourceAvailable?: boolean
}

interface OwnedPreview {
  owner: string
  preview: DailyReportPreviewDocument
  sourceDigest: string
  /** Absent from a pre-R44 server; never carried across an owner change. */
  contextCatalog: DailyReportContextCatalog | undefined
}

function ownerBoundFlags(
  generation: { isError: boolean; isPending: boolean; variables?: string },
  owner: string,
) {
  return {
    failed: generation.isError && generation.variables === owner,
    pending: generation.isPending && generation.variables === owner,
  }
}

/** Backend preview digest is review_projection(date, 1): the day, not the week. */
export function reviewDaySourceToken(review: Pick<ReviewProjection, 'day'> | undefined) {
  return review ? JSON.stringify(review.day) : ''
}

/** Weekly digest is canonical({end_date, weekly}); the day clock is not the source. */
export function reviewWeeklySourceToken(
  review: Pick<ReviewProjection, 'weekly'> | undefined,
) {
  return review ? JSON.stringify(review.weekly) : ''
}

export type ReviewSourceSelector = (
  review: ReviewProjection | undefined,
) => string

function isActiveReviewQuery(queryKey: readonly unknown[], date: string) {
  return queryKey[0] === 'review' && queryKey[1] === date
}

/**
 * An owner identity that never repeats. The workspace, day, and review
 * source generation can come back (A to B to A), so every observed change -
 * including a synchronous cache swap with no intermediate render - advances a
 * counter that a returning coordinate cannot reproduce.
 *
 * `sourceToken` is optional: daily passes `reviewDaySourceToken`, weekly
 * passes `reviewWeeklySourceToken`, and checkpoint transitions omit it.
 */
export function useOwnerGeneration(
  queryClient: QueryClient,
  workspaceId: string,
  date: string,
  sourceUpdatedAt: number | string = 0,
  sourceToken?: ReviewSourceSelector,
): { owner: string; currentOwner: () => string } {
  const coordinate = `${workspaceId}|${date}|${sourceUpdatedAt}`
  // A ref, not state: ownership must already have changed when a control
  // fires inside the same React batch as the cache write.
  const generation = useRef(0)
  const [, forceRender] = useState(0)
  const lastCoordinate = useRef(coordinate)
  const lastCached = useRef<string | undefined>(undefined)
  const lastSourceToken = useRef('')
  const identity = useRef(`${coordinate}#0`)

  const advance = (next: string) => {
    generation.current += 1
    identity.current = `${next}#${generation.current}`
  }

  if (lastCoordinate.current !== coordinate) {
    lastCoordinate.current = coordinate
    advance(coordinate)
  }

  useEffect(() => {
    lastCached.current = queryClient.getQueryData<WorkspaceProjection>(['workspace'])?.workspace.id
    if (sourceToken) {
      lastSourceToken.current = sourceToken(
        queryClient.getQueryData<ReviewProjection>(['review', date, 7]),
      )
    }
    return queryClient.getQueryCache().subscribe(({ query }) => {
      const key = query.queryKey
      if (key.length === 1 && key[0] === 'workspace') {
        const next = queryClient.getQueryData<WorkspaceProjection>(['workspace'])?.workspace.id
        if (next === lastCached.current) return
        lastCached.current = next
        advance(lastCoordinate.current)
        forceRender((value) => value + 1)
        return
      }
      if (!sourceToken || !isActiveReviewQuery(key, date)) return
      const review = queryClient.getQueryData<ReviewProjection>(key)
      // Loading/error without a retained projection is lifecycle, not a new source.
      if (!review) return
      const next = sourceToken(review)
      if (next === lastSourceToken.current) return
      lastSourceToken.current = next
      advance(lastCoordinate.current)
      forceRender((value) => value + 1)
    })
  }, [date, queryClient, sourceToken])

  return { owner: identity.current, currentOwner: () => identity.current }
}

function downloadMarkdown(preview: DailyReportPreviewDocument) {
  const blob = new Blob([preview.markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `workstack-daily-${preview.period.date}.md`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function generateButtonLabel(pending: boolean, failed: boolean) {
  if (pending) return 'Generating…'
  if (failed) return 'Retry report'
  return 'Generate report'
}

function DailyReportResult({
  actionError,
  contextCatalog,
  copyState,
  onCopy,
  onDownload,
  onEdit,
  preview,
}: {
  actionError: string | null
  contextCatalog: DailyReportContextCatalog | undefined
  copyState: 'idle' | 'copied'
  onCopy: () => void
  onDownload: () => void
  onEdit: () => void
  preview: DailyReportPreviewDocument
}) {
  return (
    <>
      <p className="daily-report-preview__meta">
        {formatDate(preview.period.date, preview.period.date)}
        {' · '}
        Generated {formatDateTime(preview.generated_at)}
      </p>
      {preview.absence === 'no records' ? (
        <p className="daily-report-preview__absence">No records for this day.</p>
      ) : null}
      <DailyReportDocument markdown={preview.markdown} />
      <div className="daily-report-preview__actions">
        <Button onClick={onCopy} variant="secondary">
          {copyState === 'copied' ? 'Markdown copied' : 'Copy Markdown'}
        </Button>
        <Button onClick={onDownload} variant="secondary">Download .md</Button>
        <Button onClick={onEdit} variant="secondary">Edit local draft</Button>
      </div>
      {actionError ? (
        <p className="daily-report-preview__status" data-tone="error" role="alert">{actionError}</p>
      ) : null}
      {contextCatalog ? <DailyReportContextPanel catalog={contextCatalog} /> : null}
    </>
  )
}

function staleOwnerMessage() {
  return 'This report is no longer for the selected day and workspace.'
}

async function copyOwnedMarkdown(
  owned: DailyReportPreviewDocument | null,
  resultOwner: string | undefined,
  currentOwner: () => string,
  setActionError: (value: string | null) => void,
  setCopyState: (value: 'idle' | 'copied') => void,
) {
  const frozenOwner = currentOwner()
  if (!owned || resultOwner !== frozenOwner) {
    setActionError(staleOwnerMessage())
    return
  }
  try {
    await copyTextToClipboard(owned.markdown)
    if (frozenOwner !== currentOwner()) return
    setActionError(null)
    setCopyState('copied')
  } catch {
    if (frozenOwner !== currentOwner()) return
    setCopyState('idle')
    setActionError('Markdown could not be copied.')
  }
}

function saveOwnedMarkdown(
  owned: DailyReportPreviewDocument | null,
  resultOwner: string | undefined,
  currentOwner: string,
  setActionError: (value: string | null) => void,
) {
  if (!owned || resultOwner !== currentOwner) {
    setActionError(staleOwnerMessage())
    return
  }
  try {
    downloadMarkdown(owned)
    setActionError(null)
  } catch {
    setActionError('The markdown file could not be downloaded.')
  }
}

interface DraftSession {
  coordinate: DailyReportDraftEditorProps['coordinate']
  source: DailyReportDraftSource
}

function useDraftEditorSession(
  workspaceId: string,
  date: string,
  owner: string,
  currentOwner: () => string,
  result: OwnedPreview | null,
  owned: DailyReportPreviewDocument | null,
  setActionError: (value: string | null) => void,
) {
  const [session, setSession] = useState<DraftSession | null>(null)

  useEffect(() => {
    if (!owned || !result || result.owner !== owner) return
    setSession((current) => {
      if (!current) return current
      if (current.coordinate.workspaceUid !== workspaceId || current.coordinate.date !== date) {
        return current
      }
      const source = {
        sourceDigest: result.sourceDigest,
        generatedAt: owned.generated_at,
        markdown: owned.markdown,
      }
      if (
        current.source.sourceDigest === source.sourceDigest
        && current.source.generatedAt === source.generatedAt
        && current.source.markdown === source.markdown
      ) return current
      return { ...current, source }
    })
  }, [date, owned, owner, result, workspaceId])

  const open = () => {
    if (!owned || !result || result.owner !== currentOwner()) {
      setActionError(staleOwnerMessage())
      return
    }
    setSession({
      coordinate: { workspaceUid: workspaceId, date: owned.period.date, template: 'daily-v1' },
      source: {
        sourceDigest: result.sourceDigest,
        generatedAt: owned.generated_at,
        markdown: owned.markdown,
      },
    })
  }

  return { session, open, close: () => setSession(null) }
}

function DailyReportPreviewPanel({
  actionError,
  contextCatalog,
  copyState,
  currentFailed,
  currentPending,
  currentOwner,
  date,
  owned,
  onEdit,
  onGenerate,
  resultOwner,
  setActionError,
  setCopyState,
}: {
  actionError: string | null
  contextCatalog: DailyReportContextCatalog | undefined
  copyState: 'idle' | 'copied'
  currentFailed: boolean
  currentPending: boolean
  currentOwner: () => string
  date: string
  owned: DailyReportPreviewDocument | null
  onEdit: () => void
  onGenerate: () => void
  resultOwner: string | undefined
  setActionError: (value: string | null) => void
  setCopyState: (value: 'idle' | 'copied') => void
}) {
  return (
    <section className="daily-report-preview" aria-labelledby="daily-report-heading">
      <header className="daily-report-preview__header">
        <div>
          <h2 id="daily-report-heading">Daily report</h2>
          <p>Build a readable preview for {formatDate(date, date)}. Nothing is generated until you ask.</p>
        </div>
        <div className="daily-report-preview__actions">
          <Button disabled={currentPending} onClick={onGenerate} variant="primary">
            {generateButtonLabel(currentPending, currentFailed)}
          </Button>
        </div>
      </header>
      {currentPending ? <LoadingBlock label="Generating the daily report…" /> : null}
      {actionError && !owned ? <ErrorState message={actionError} onRetry={onGenerate} /> : null}
      {owned ? (
        <DailyReportResult
          actionError={actionError}
          contextCatalog={contextCatalog}
          copyState={copyState}
          onCopy={() => void copyOwnedMarkdown(owned, resultOwner, currentOwner, setActionError, setCopyState)}
          onDownload={() => saveOwnedMarkdown(owned, resultOwner, currentOwner(), setActionError)}
          onEdit={onEdit}
          preview={owned}
        />
      ) : null}
    </section>
  )
}

export function DailyReportPreview({
  date,
  sourceAvailable = true,
  sourceUpdatedAt,
  workspaceId,
}: DailyReportPreviewProps) {
  const queryClient = useQueryClient()
  const { owner, currentOwner } = useOwnerGeneration(
    queryClient,
    workspaceId,
    date,
    sourceUpdatedAt,
    reviewDaySourceToken,
  )
  const [result, setResult] = useState<OwnedPreview | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [copyState, setCopyState] = useState<'idle' | 'copied'>('idle')

  const generation = useMutation({
    mutationFn: (frozenOwner: string) => (
      api.getDailyReportPreview(date, workspaceId).then((payload: DailyReportPreviewResponse) => (
        { frozenOwner, payload }
      ))
    ),
    onSuccess: ({ frozenOwner, payload }) => {
      if (frozenOwner !== currentOwner()) return
      setResult({
        owner: frozenOwner,
        preview: payload.preview,
        sourceDigest: payload.source_digest,
        contextCatalog: payload.context_catalog,
      })
      setActionError(null)
      setCopyState('idle')
    },
    onError: (error: unknown, frozenOwner: string) => {
      if (frozenOwner !== currentOwner()) return
      setResult(null)
      setActionError(getErrorMessage(error))
      setCopyState('idle')
    },
  })

  useEffect(() => {
    setResult(null)
    setActionError(null)
    setCopyState('idle')
    generation.reset()
  }, [owner])

  const owned = result && result.owner === owner ? result.preview : null
  const { failed: currentFailed, pending: currentPending } = ownerBoundFlags(generation, owner)
  const draft = useDraftEditorSession(
    workspaceId,
    date,
    owner,
    currentOwner,
    result,
    owned,
    setActionError,
  )

  const generate = () => {
    if (!sourceAvailable) return
    setActionError(null)
    setCopyState('idle')
    generation.mutate(currentOwner())
  }

  return (
    <>
      {sourceAvailable ? (
        <DailyReportPreviewPanel
          actionError={actionError}
          contextCatalog={owned ? result?.contextCatalog : undefined}
          copyState={copyState}
          currentFailed={currentFailed}
          currentPending={currentPending}
          currentOwner={currentOwner}
          date={date}
          owned={owned}
          onEdit={draft.open}
          onGenerate={generate}
          resultOwner={result?.owner}
          setActionError={setActionError}
          setCopyState={setCopyState}
        />
      ) : null}
      {draft.session ? (
        <DailyReportDraftEditor
          coordinate={draft.session.coordinate}
          onClose={draft.close}
          source={draft.session.source}
        />
      ) : null}
    </>
  )
}
