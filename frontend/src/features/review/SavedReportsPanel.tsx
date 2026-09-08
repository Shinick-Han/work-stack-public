import { useState } from 'react'
import { Button, EmptyState, LoadingBlock, Pill } from '../../components/Primitives'
import { REPORT_LIST_FILTERS, type ReportContentEntry, type ReportListFilter, type ReportListItem, type ReportReadData } from '../../domain/reportDocuments'
import { ReportRevisionDialog } from './ReportRevisionDialog'
import { formatDate, formatDateTime } from '../../utils/format'
import {
  EMPTY_SAVED_REPORTS,
  FINALIZE_DISCLAIMER,
  INERT_HISTORY_HINT,
  SOURCE_STALE_MESSAGE,
  filterLabel,
  statusLabel,
} from './savedReportsMessages'
import { allowedTransitions } from './savedReportsModel'
import { useSavedReports, type SavedReportsModel } from './useSavedReports'
import './savedReportsPanel.css'

function FilterBar({
  disabled,
  filter,
  onFilter,
}: {
  disabled: boolean
  filter: ReportListFilter
  onFilter: (filter: ReportListFilter) => void
}) {
  return (
    <div className="saved-reports__filters" role="toolbar" aria-label="Saved report filters">
      {REPORT_LIST_FILTERS.map((value) => (
        <Button
          aria-pressed={filter === value}
          disabled={disabled}
          key={value}
          onClick={() => onFilter(value)}
          variant={filter === value ? 'primary' : 'secondary'}
        >
          {filterLabel(value)}
        </Button>
      ))}
    </div>
  )
}

function ReportRow({
  disabled,
  item,
  selected,
  onSelect,
}: {
  disabled: boolean
  item: ReportListItem
  selected: boolean
  onSelect: (uid: string) => void
}) {
  return (
    <button
      aria-current={selected ? 'true' : undefined}
      className={selected ? 'saved-reports__item is-selected' : 'saved-reports__item'}
      disabled={disabled}
      onClick={() => onSelect(item.uid)}
      type="button"
    >
      <strong>{formatDate(item.period.date, item.period.date)}</strong>
      <span><Pill tone={item.state === 'finalized' ? 'done' : item.state === 'archived' ? 'neutral' : 'open'}>{statusLabel(item.state)}</Pill></span>
      <small>Report version {item.content_revision} · Updated {formatDateTime(item.updated_at)}</small>
    </button>
  )
}

function AuthoredMarkdown({ label, markdown }: { label: string; markdown: string }) {
  return (
    <pre aria-label={label} className="saved-reports__body" tabIndex={0}>{markdown}</pre>
  )
}

function HistoryList({ revisions }: { revisions: ReportContentEntry[] }) {
  return (
    <div className="saved-reports__history">
      <h3>Authored versions</h3>
      <p>{INERT_HISTORY_HINT}</p>
      {revisions.map((entry) => (
        <article key={entry.content_revision}>
          <p className="saved-reports__meta">
            Report version {entry.content_revision}
            {' · '}
            {formatDateTime(entry.authored_at)}
            {entry.note ? ` · ${entry.note}` : ''}
          </p>
          <AuthoredMarkdown label={`Authored markdown version ${entry.content_revision}`} markdown={entry.markdown} />
        </article>
      ))}
    </div>
  )
}

function ActionBar({
  editing,
  model,
  onEdit,
}: {
  editing: boolean
  model: SavedReportsModel
  onEdit: () => void
}) {
  const document = model.document
  if (!document) return null
  const allowed = allowedTransitions(document.state)
  const locked = model.transitionPending || model.needsReconcile || model.retryable
  const archived = document.state === 'archived'
  return (
    <div className="saved-reports__actions">
      <Button
        disabled={locked || archived || editing}
        onClick={onEdit}
        variant="secondary"
      >
        Edit report
      </Button>
      {allowed.includes('finalize') ? (
        <Button disabled={locked} onClick={model.finalize} variant="primary">
          {model.transitionPending ? 'Working…' : 'Finalize'}
        </Button>
      ) : null}
      {allowed.includes('archive') ? (
        <Button disabled={locked} onClick={model.archive} variant="secondary">Archive</Button>
      ) : null}
      {allowed.includes('restore') ? (
        <Button disabled={locked} onClick={model.restore} variant="secondary">Restore</Button>
      ) : null}
      {model.retryable ? (
        <Button onClick={model.retry} variant="primary">Retry the same request</Button>
      ) : null}
      {model.needsReconcile ? (
        <Button onClick={model.refreshDocument} variant="secondary">Refresh this report</Button>
      ) : null}
    </div>
  )
}

function SavedReportDetail({ document }: { document: ReportReadData }) {
  const latest = document.revisions.at(-1)
  return (
    <div className="saved-reports__detail">
      <h3>{formatDate(document.period.date, document.period.date)}</h3>
      <p className="saved-reports__meta">
        <Pill tone={document.state === 'finalized' ? 'done' : document.state === 'archived' ? 'neutral' : 'open'}>{statusLabel(document.state)}</Pill>
        {' · '}
        <span className="saved-reports__version">Report version {document.content_revision}</span>
        {document.archived_from_state ? ` · Archived from ${statusLabel(document.archived_from_state)}` : ''}
      </p>
      {document.source_stale ? <p className="saved-reports__status" role="status">{SOURCE_STALE_MESSAGE}</p> : null}
      {latest ? <AuthoredMarkdown label="Current authored markdown" markdown={latest.markdown} /> : null}
      <HistoryList revisions={document.revisions} />
    </div>
  )
}

function SavedReportsList({ model }: { model: SavedReportsModel }) {
  if (model.listPending && !model.items.length) return <LoadingBlock label="Opening saved reports…" />
  if (!model.listPending && !model.items.length && !model.listError && !model.document) {
    return <EmptyState title="No saved reports">{EMPTY_SAVED_REPORTS}</EmptyState>
  }
  if (!model.items.length) return null
  return (
    <div className="saved-reports__list" role="list">
      {model.items.map((item) => (
        <ReportRow
          disabled={model.transitionPending}
          item={item}
          key={item.uid}
          onSelect={model.select}
          selected={model.selectedUid === item.uid}
        />
      ))}
    </div>
  )
}

function SavedReportsBody({
  editing,
  model,
  onEdit,
}: {
  editing: boolean
  model: SavedReportsModel
  onEdit: () => void
}) {
  return (
    <>
      {model.listError ? <p className="saved-reports__status" data-tone="error" role="alert">{model.listError}</p> : null}
      <SavedReportsList model={model} />
      <div className="saved-reports__list-actions">
        {model.canLoadMore ? (
          <Button disabled={model.morePending} onClick={model.loadMore} variant="secondary">
            {model.morePending ? 'Loading…' : `Load more (${model.omittedCount} more)`}
          </Button>
        ) : null}
        <Button disabled={model.listPending || model.morePending} onClick={model.refreshList} variant="ghost">
          Refresh list
        </Button>
      </div>
      {model.readPending && !model.document ? <LoadingBlock label="Opening report history…" /> : null}
      {model.readError ? <p className="saved-reports__status" data-tone="error" role="alert">{model.readError}</p> : null}
      {model.transitionError ? <p className="saved-reports__status" data-tone="error" role="alert">{model.transitionError}</p> : null}
      {model.document ? <SavedReportDetail document={model.document} /> : null}
      <ActionBar editing={editing} model={model} onEdit={onEdit} />
    </>
  )
}

export function SavedReportsPanel({ workspaceId }: { workspaceId: string }) {
  const model = useSavedReports(workspaceId)
  const [revisionDocument, setRevisionDocument] = useState<ReportReadData | null>(null)
  return (
    <section className="saved-reports" aria-labelledby="saved-reports-heading">
      <header className="saved-reports__header">
        <div>
          <h2 id="saved-reports-heading">Saved reports</h2>
          <p>{FINALIZE_DISCLAIMER}</p>
        </div>
        <FilterBar disabled={model.listPending || model.morePending} filter={model.filter} onFilter={model.setFilter} />
      </header>
      {model.admitted ? (
        <SavedReportsBody
          editing={revisionDocument !== null}
          model={model}
          onEdit={() => {
            if (model.document && model.document.state !== 'archived') setRevisionDocument(model.document)
          }}
        />
      ) : (
        <EmptyState title="No saved reports">{EMPTY_SAVED_REPORTS}</EmptyState>
      )}
      {revisionDocument ? (
        <ReportRevisionDialog
          document={revisionDocument}
          liveSelectedUid={model.selectedUid}
          liveWorkspaceId={workspaceId}
          onClose={() => setRevisionDocument(null)}
          onSaved={() => {
            model.refreshDocument()
            model.refreshList()
          }}
        />
      ) : null}
    </section>
  )
}
