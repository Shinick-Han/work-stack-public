import { useEffect, useId, useRef } from 'react'
import { Dialog } from '../../components/Dialog'
import { Button, Pill } from '../../components/Primitives'
import {
  MAX_REPORT_MARKDOWN_CHARS,
  MAX_REPORT_NOTE_CHARS,
} from '../../domain/reportDocuments'
import { formatDate } from '../../utils/format'
import { SOURCE_STALE_MESSAGE, statusLabel } from './savedReportsMessages'
import {
  ARCHIVED_LOCKED,
  DIRTY_CLOSE_BODY,
  REVISION_DIALOG_DESCRIPTION,
  STALE_OWNER_MESSAGE,
  codePointCount,
} from './reportRevisionModel'
import { useReportRevision, type ReportRevisionInput, type ReportRevisionModel } from './useReportRevision'
import './reportRevisionDialog.css'

function DiscardPrompt({ model }: { model: ReportRevisionModel }) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const { actions } = model

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal()
    const first = dialog.querySelector('button')
    if (first instanceof HTMLElement) first.focus()
    return () => {
      if (dialog.open && typeof dialog.close === 'function') dialog.close()
    }
  }, [])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const handleCancel = (event: Event) => {
      event.preventDefault()
      actions.cancelPrompt()
    }
    dialog.addEventListener('cancel', handleCancel)
    return () => dialog.removeEventListener('cancel', handleCancel)
  }, [actions])

  return (
    <dialog
      aria-labelledby={titleId}
      className="report-revision__confirm"
      ref={dialogRef}
      role="alertdialog"
    >
      <h3 id={titleId}>Discard unsaved changes</h3>
      <p>{DIRTY_CLOSE_BODY}</p>
      <div className="report-revision__confirm-actions">
        <Button disabled={!model.canExport} onClick={actions.copy}>Copy Markdown</Button>
        <Button disabled={!model.canExport} onClick={actions.download}>Download .md</Button>
        <Button onClick={actions.confirmDiscard} variant="danger">Discard changes and close</Button>
        <Button onClick={actions.cancelPrompt} variant="secondary">Keep editing</Button>
      </div>
    </dialog>
  )
}

function RevisionNotices({ model }: { model: ReportRevisionModel }) {
  const { state, staleOwner, validation } = model
  const opened = statusLabel(state.pin.openedState)
  return (
    <>
      <p className="report-revision__meta">
        <Pill tone={state.pin.openedState === 'finalized' ? 'done' : state.pin.openedState === 'archived' ? 'neutral' : 'open'}>
          {opened}
        </Pill>
        {' · '}
        {formatDate(state.pin.periodDate, state.pin.periodDate)}
        {' · '}
        Report version {state.pin.contentRevision}
      </p>
      {staleOwner ? (
        <p className="report-revision__banner" role="status">
          {STALE_OWNER_MESSAGE}
          {' '}
          Original report: {formatDate(state.pin.periodDate, state.pin.periodDate)}.
        </p>
      ) : null}
      {state.pin.openedState === 'archived' ? (
        <p className="report-revision__notice" role="alert">{ARCHIVED_LOCKED}</p>
      ) : null}
      {state.pin.sourceStale ? (
        <p className="report-revision__banner" role="status">{SOURCE_STALE_MESSAGE}</p>
      ) : null}
      {state.notice ? <p className="report-revision__notice" role="alert">{state.notice}</p> : null}
      {state.status ? <p className="report-revision__status" role="status">{state.status}</p> : null}
      {!state.notice && validation ? <p className="report-revision__notice" role="alert">{validation}</p> : null}
    </>
  )
}

function RevisionBody({ model }: { model: ReportRevisionModel }) {
  const countId = useId()
  const { actions, prompting, staleOwner, state } = model
  const locked = prompting || state.saving || staleOwner
  return (
    <div className="report-revision" inert={prompting || undefined}>
      <RevisionNotices model={model} />
      <label className="report-revision__field">
        <span>Report markdown</span>
        <textarea
          aria-describedby={countId}
          aria-label="Report markdown"
          className="report-revision__textarea"
          disabled={locked}
          onChange={(event) => actions.setText(event.target.value)}
          spellCheck={false}
          value={state.text}
        />
      </label>
      <p className="report-revision__count" id={countId}>
        {codePointCount(state.text).toLocaleString()} of {MAX_REPORT_MARKDOWN_CHARS.toLocaleString()} characters
        {model.dirty ? ' · unsaved changes' : ' · no unsaved changes'}
      </p>
      <label className="report-revision__field">
        <span>Version note (optional)</span>
        <input
          aria-label="Version note"
          className="report-revision__note"
          disabled={locked}
          onChange={(event) => actions.setNote(event.target.value)}
          value={state.note}
        />
      </label>
      <p className="report-revision__count">
        {codePointCount(state.note).toLocaleString()} of {MAX_REPORT_NOTE_CHARS.toLocaleString()} characters
      </p>
      {model.staleOwner ? (
        <pre aria-label="Original report markdown" className="report-revision__snapshot" tabIndex={0}>
          {state.text}
        </pre>
      ) : null}
    </div>
  )
}

function RevisionFooter({ model }: { model: ReportRevisionModel }) {
  const { actions, state } = model
  const closeLocked = state.saving || model.prompting
  return (
    <div className="report-revision__footer">
      <Button disabled={!model.canSave} onClick={actions.save} variant="primary">
        {state.saving ? 'Saving…' : 'Save new version'}
      </Button>
      {model.canRetry ? (
        <Button onClick={actions.retry} variant="primary">Retry the same request</Button>
      ) : null}
      <Button disabled={!model.canExport} onClick={actions.copy}>Copy Markdown</Button>
      <Button disabled={!model.canExport} onClick={actions.download}>Download .md</Button>
      <Button
        className="report-revision__close"
        disabled={closeLocked}
        onClick={actions.requestClose}
      >
        Close
      </Button>
    </div>
  )
}

export function ReportRevisionDialog(props: ReportRevisionInput) {
  const model = useReportRevision(props)
  return (
    <Dialog
      description={REVISION_DIALOG_DESCRIPTION}
      footer={<RevisionFooter model={model} />}
      onClose={model.actions.requestClose}
      open
      size="large"
      title="Edit saved report"
    >
      {model.prompting ? <DiscardPrompt model={model} /> : null}
      <RevisionBody model={model} />
    </Dialog>
  )
}
