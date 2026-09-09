import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Button, ErrorState, LoadingBlock } from '../../components/Primitives'
import type { DailyReportContextCatalog } from '../../domain/reporting'
import type {
  WeeklyReportPreviewDocument,
  WeeklyReportPreviewResponse,
} from '../../domain/weeklyReporting'
import { formatDate, formatDateTime, getErrorMessage } from '../../utils/format'
import { copyTextToClipboard } from '../../utils/clipboard'
import { DailyReportContextPanel } from './DailyReportContextPanel'
import { DailyReportDocument } from './DailyReportDocument'
import { reviewWeeklySourceToken, useOwnerGeneration } from './DailyReportPreview'
import './weeklyReportPreview.css'

interface WeeklyReportPreviewProps {
  endDate: string
  workspaceId: string
  sourceUpdatedAt: number | string
  /** When false, the generate panel is gated; an owned result stays recovered. */
  sourceAvailable?: boolean
}

interface OwnedPreview {
  owner: string
  preview: WeeklyReportPreviewDocument
  /** Absent from a pre-R45 server; never carried across an owner change. */
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

function generateButtonLabel(pending: boolean, failed: boolean) {
  if (pending) return 'Generating…'
  if (failed) return 'Retry weekly report'
  return 'Generate weekly report'
}

function staleOwnerMessage() {
  return 'This report is no longer for the selected week and workspace.'
}

function downloadWeeklyMarkdown(preview: WeeklyReportPreviewDocument) {
  const blob = new Blob([preview.markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `workstack-weekly-${preview.period.start}-to-${preview.period.end}.md`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

async function copyOwnedMarkdown(
  owned: WeeklyReportPreviewDocument | null,
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
  owned: WeeklyReportPreviewDocument | null,
  resultOwner: string | undefined,
  currentOwner: string,
  setActionError: (value: string | null) => void,
) {
  if (!owned || resultOwner !== currentOwner) {
    setActionError(staleOwnerMessage())
    return
  }
  try {
    downloadWeeklyMarkdown(owned)
    setActionError(null)
  } catch {
    setActionError('The markdown file could not be downloaded.')
  }
}

function WeeklyReportResult({
  actionError,
  contextCatalog,
  copyState,
  onCopy,
  onDownload,
  preview,
}: {
  actionError: string | null
  contextCatalog: DailyReportContextCatalog | undefined
  copyState: 'idle' | 'copied'
  onCopy: () => void
  onDownload: () => void
  preview: WeeklyReportPreviewDocument
}) {
  return (
    <>
      <p className="weekly-report-preview__meta">
        {formatDate(preview.period.start, preview.period.start)}
        {' → '}
        {formatDate(preview.period.end, preview.period.end)}
        {' · '}
        Generated {formatDateTime(preview.generated_at)}
      </p>
      {preview.absence === 'no records' ? (
        <p className="weekly-report-preview__absence">No records for this week.</p>
      ) : null}
      <DailyReportDocument markdown={preview.markdown} />
      <div className="weekly-report-preview__actions">
        <Button onClick={onCopy} variant="secondary">
          {copyState === 'copied' ? 'Markdown copied' : 'Copy Markdown'}
        </Button>
        <Button onClick={onDownload} variant="secondary">Download .md</Button>
      </div>
      {actionError ? (
        <p className="weekly-report-preview__status" data-tone="error" role="alert">{actionError}</p>
      ) : null}
      {contextCatalog ? <DailyReportContextPanel catalog={contextCatalog} /> : null}
    </>
  )
}

function WeeklyReportPreviewPanel({
  actionError,
  contextCatalog,
  copyState,
  currentFailed,
  currentPending,
  currentOwner,
  endDate,
  owned,
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
  endDate: string
  owned: WeeklyReportPreviewDocument | null
  onGenerate: () => void
  resultOwner: string | undefined
  setActionError: (value: string | null) => void
  setCopyState: (value: 'idle' | 'copied') => void
}) {
  return (
    <section className="weekly-report-preview" aria-labelledby="weekly-report-heading">
      <header className="weekly-report-preview__header">
        <div>
          <h2 id="weekly-report-heading">Weekly report</h2>
          <p>
            Build a readable preview for the week ending {formatDate(endDate, endDate)}.
            Nothing is generated until you ask.
          </p>
        </div>
        <div className="weekly-report-preview__actions">
          <Button disabled={currentPending} onClick={onGenerate} variant="primary">
            {generateButtonLabel(currentPending, currentFailed)}
          </Button>
        </div>
      </header>
      {currentPending ? <LoadingBlock label="Generating the weekly report…" /> : null}
      {actionError && !owned ? <ErrorState message={actionError} onRetry={onGenerate} /> : null}
      {owned ? (
        <WeeklyReportResult
          actionError={actionError}
          contextCatalog={contextCatalog}
          copyState={copyState}
          onCopy={() => void copyOwnedMarkdown(owned, resultOwner, currentOwner, setActionError, setCopyState)}
          onDownload={() => saveOwnedMarkdown(owned, resultOwner, currentOwner(), setActionError)}
          preview={owned}
        />
      ) : null}
    </section>
  )
}

export function WeeklyReportPreview({
  endDate,
  sourceAvailable = true,
  sourceUpdatedAt,
  workspaceId,
}: WeeklyReportPreviewProps) {
  const queryClient = useQueryClient()
  const { owner, currentOwner } = useOwnerGeneration(
    queryClient,
    workspaceId,
    endDate,
    sourceUpdatedAt,
    reviewWeeklySourceToken,
  )
  const [result, setResult] = useState<OwnedPreview | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [copyState, setCopyState] = useState<'idle' | 'copied'>('idle')

  const generation = useMutation({
    mutationFn: (frozenOwner: string) => (
      api.getWeeklyReportPreview(endDate, workspaceId).then(
        (payload: WeeklyReportPreviewResponse) => ({ frozenOwner, payload }),
      )
    ),
    onSuccess: ({ frozenOwner, payload }) => {
      if (frozenOwner !== currentOwner()) return
      setResult({
        owner: frozenOwner,
        preview: payload.preview,
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

  const generate = () => {
    if (!sourceAvailable) return
    setActionError(null)
    setCopyState('idle')
    generation.mutate(currentOwner())
  }

  return sourceAvailable ? (
    <WeeklyReportPreviewPanel
      actionError={actionError}
      contextCatalog={owned ? result?.contextCatalog : undefined}
      copyState={copyState}
      currentFailed={currentFailed}
      currentPending={currentPending}
      currentOwner={currentOwner}
      endDate={endDate}
      owned={owned}
      onGenerate={generate}
      resultOwner={result?.owner}
      setActionError={setActionError}
      setCopyState={setCopyState}
    />
  ) : null
}
