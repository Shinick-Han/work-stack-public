import { useEffect, useId, useRef, useState } from 'react'

import { Button } from '../../components/Primitives'
import { FALLBACK_ADVICE, staleSourceMessage } from './reportDraftMessages'
import {
  isRegeneratedUnchanged,
  isStaleSource,
  type DailyReportDraftSource,
  type DraftSessionState,
} from './reportDraftEditorModel'
import { REPORT_DRAFT_MAX_MARKDOWN, type ReportDraftCoordinate } from './reportDraftStorage'
import type { ReportDraftEditorModel } from './useReportDraftEditor'

/** Presentation for the local report draft editor. No storage, no async work. */

/**
 * A modal confirmation in its own top layer.
 *
 * It is a nested native dialog rather than an inline panel, so the browser puts
 * it above the editor and keeps interaction inside it. The editor's own controls
 * are disabled independently, because a prompt that merely LOOKS blocking would
 * still let someone edit and save underneath it and then confirm against state
 * that no longer exists.
 */
export function ConfirmationDialog({
  body,
  confirmLabel,
  onCancel,
  onConfirm,
  title,
}: {
  body: string
  confirmLabel: string
  onCancel: () => void
  onConfirm: () => void
  title: string
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()

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
      onCancel()
    }
    dialog.addEventListener('cancel', handleCancel)
    return () => dialog.removeEventListener('cancel', handleCancel)
  }, [onCancel])

  return (
    <dialog
      aria-labelledby={titleId}
      className="report-draft-editor__confirm"
      ref={dialogRef}
      role="alertdialog"
    >
      <h3 id={titleId}>{title}</h3>
      <p>{body}</p>
      <div className="report-draft-editor__confirm-actions">
        <Button onClick={onConfirm} variant="danger">{confirmLabel}</Button>
        <Button onClick={onCancel} variant="secondary">Keep editing</Button>
      </div>
    </dialog>
  )
}

export function DraftPrompts({ model }: { model: ReportDraftEditorModel }) {
  const { actions, state } = model
  if (state.confirmation?.kind === 'discard') {
    return (
      <ConfirmationDialog
        body="This draft has unsaved changes. Closing now discards them on this device. Copy Markdown or Download .md first if you want to keep them."
        confirmLabel="Discard changes and close"
        onCancel={actions.cancelPrompt}
        onConfirm={actions.confirmDiscard}
        title="Discard unsaved changes"
      />
    )
  }
  if (state.confirmation?.kind === 'delete') {
    const revision = state.confirmation.revision
    return (
      <ConfirmationDialog
        body={
          'Delete the saved draft for this report on this device? Revision '
          + String(revision)
          + ' is removed. The text in the editor stays exactly as it is and becomes unsaved.'
        }
        confirmLabel="Delete the saved draft"
        onCancel={actions.cancelPrompt}
        onConfirm={() => actions.confirmDelete(revision)}
        title="Delete saved draft"
      />
    )
  }
  return null
}

export function DraftFooter({ model }: { model: ReportDraftEditorModel }) {
  const { actions, prompting, state } = model
  const overLimit = state.text.length > REPORT_DRAFT_MAX_MARKDOWN
  // Writing is serialized, so Save and Delete wait for an in-flight operation.
  const writing = state.busy || state.loading || prompting
  // Keeping the text is NOT. Copy and Download read what is on screen and touch
  // no storage, so a hung save must not take away the two ways out of it; they
  // stop only when there is no text to export yet, or a prompt owns the surface.
  const exporting = state.loading || prompting
  return (
    <div className="report-draft-editor__footer">
      <Button disabled={writing || overLimit} onClick={actions.save} variant="primary">
        Save local draft
      </Button>
      <Button disabled={exporting} onClick={actions.copy}>Copy Markdown</Button>
      <Button disabled={exporting} onClick={actions.download}>Download .md</Button>
      <Button
        disabled={writing || state.localRevision === null}
        onClick={actions.promptDelete}
        variant="danger"
      >
        Delete saved draft
      </Button>
      <Button
        className="report-draft-editor__close"
        disabled={state.busy || prompting}
        onClick={actions.requestClose}
      >
        Close
      </Button>
    </div>
  )
}

function DraftNotices({
  base,
  notice,
  noticeId,
  source,
  status,
}: {
  base: DraftSessionState['base']
  notice: string | null
  noticeId: string
  source: DailyReportDraftSource
  status: string | null
}) {
  return (
    <>
      {isStaleSource(base, source) && base !== null ? (
        <p className="report-draft-editor__stale" role="status">
          {staleSourceMessage(base.baseGeneratedAt)}
        </p>
      ) : null}
      {isRegeneratedUnchanged(base, source) ? (
        <p className="report-draft-editor__regenerated" role="status">
          This report was generated again at {source.generatedAt} from the same facts this
          draft started from, so the draft is not out of date.
        </p>
      ) : null}
      {notice ? (
        <p className="report-draft-editor__notice" id={noticeId} role="alert">{notice}</p>
      ) : null}
      {status ? (
        <p className="report-draft-editor__status" role="status">{status}</p>
      ) : null}
    </>
  )
}

function DraftReadingView({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="report-draft-editor__render">
      <Button aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        {open ? 'Hide plain reading view' : 'Show plain reading view'}
      </Button>
      {open ? (
        // Text nodes only: the draft is untrusted content and is never markup.
        <pre className="report-draft-editor__reading" tabIndex={0}>{text}</pre>
      ) : null}
    </div>
  )
}

function DraftCoordinate({ pinned }: { pinned: ReportDraftCoordinate }) {
  return (
    <p className="report-draft-editor__coordinate">
      <span>{pinned.date}</span>
      <span>Daily report</span>
    </p>
  )
}

export function DraftBody({
  model,
  source,
}: {
  model: ReportDraftEditorModel
  source: DailyReportDraftSource
}) {
  const noticeId = useId()
  const countId = useId()
  const { actions, dirty, pinned, prompting, state } = model
  const overLimit = state.text.length > REPORT_DRAFT_MAX_MARKDOWN
  return (
    <div
      aria-hidden={prompting || undefined}
      className="report-draft-editor"
      // While a prompt is open the editor beneath it is genuinely unavailable,
      // not merely covered: no typing, no focus and no action underneath.
      inert={prompting || undefined}
    >
      <DraftCoordinate pinned={pinned} />
      {state.loading ? <p role="status">Opening the saved draft…</p> : null}
      <DraftNotices
        base={state.base}
        notice={state.notice}
        noticeId={noticeId}
        source={source}
        status={state.status}
      />
      <label className="report-draft-editor__field">
        <span>Report markdown</span>
        <textarea
          aria-describedby={countId + (state.notice ? ' ' + noticeId : '')}
          className="report-draft-editor__textarea"
          disabled={state.loading || prompting}
          maxLength={REPORT_DRAFT_MAX_MARKDOWN}
          onChange={(event) => actions.setText(event.target.value)}
          spellCheck={false}
          value={state.text}
        />
      </label>
      <p className="report-draft-editor__count" id={countId}>
        {state.text.length.toLocaleString()} of{' '}
        {REPORT_DRAFT_MAX_MARKDOWN.toLocaleString()} characters
        {dirty ? ' · unsaved changes' : ' · no unsaved changes'}
        {state.localRevision === null
          ? ' · not saved on this device'
          : ' · local revision ' + String(state.localRevision)}
      </p>
      {overLimit ? (
        <p className="report-draft-editor__notice" role="alert">
          This text is longer than the local limit and cannot be saved on this device.{' '}
          {FALLBACK_ADVICE}
        </p>
      ) : null}
      <DraftReadingView text={state.text} />
    </div>
  )
}
