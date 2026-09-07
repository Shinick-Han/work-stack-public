import type { ReportDraftErrorCode } from './reportDraftStorage'

/**
 * Human guidance for the local edited-report draft buffer.
 *
 * Every message says the same three things in the reader's own terms: what
 * happened, that the text on screen is still there, and what they can do next.
 * A local draft is a convenience on ONE device, so the fallback named here is
 * always Copy Markdown or Download .md — never "try again and hope".
 *
 * Nothing here claims a Work Stack revision, a server document or a finalized
 * report. Saving writes an origin-local buffer and nothing else.
 */

export const LOCAL_ONLY_LABEL =
  'Local draft on this device only. Saving it does not change your work records or publish the report.'

export const FALLBACK_ADVICE =
  'Your text is still here. Use Copy Markdown or Download .md to keep it before you close.'

const SAVE_MESSAGES: Record<ReportDraftErrorCode, string> = {
  capacity:
    'This device has no room left for another local draft. Remove a saved draft you no longer need, then save again.',
  conflict:
    'This draft changed somewhere else on this device — another tab probably saved it. Reload the editor to see that version, or keep your text with Copy Markdown or Download .md first.',
  invalid_input:
    'This draft could not be saved because the report text is longer than the local limit, or the report identity is incomplete.',
  invalid_storage:
    'The local draft store on this device is unreadable, so saving would risk the drafts already in it. Nothing was written.',
  not_found:
    'There is no saved draft to update any more; it was removed somewhere else on this device. Save again to store the current text as a new draft.',
  revision_exhausted:
    'This draft has been saved too many times to track another revision on this device. Keep the text with Copy Markdown or Download .md and start a new draft.',
  storage_unavailable:
    'Local storage is unavailable in this browser, so nothing can be saved on this device.',
  storage_write_failed:
    'This device refused the write, so the draft was not saved. Storage may be full or restricted here.',
}

const DELETE_MESSAGES: Partial<Record<ReportDraftErrorCode, string>> = {
  conflict:
    'The saved draft changed somewhere else on this device, so it was not deleted. Reload the editor to see the current saved version.',
  not_found: 'There was no saved draft left to delete on this device.',
}

/** What went wrong while saving, and what the reader can still do about it. */
export function saveErrorMessage(code: ReportDraftErrorCode): string {
  return SAVE_MESSAGES[code] + ' ' + FALLBACK_ADVICE
}

/** Deleting the saved copy never touches the text in the editor, so say so. */
export function deleteErrorMessage(code: ReportDraftErrorCode): string {
  const detail =
    DELETE_MESSAGES[code]
    ?? 'The saved draft could not be deleted on this device.'
  return detail + ' The text in the editor was not changed.'
}

/** Loading failed, so the editor opened on the report as generated. */
export function loadErrorMessage(code: ReportDraftErrorCode): string {
  if (code === 'invalid_storage') {
    // Saving reads and rewrites the whole local envelope, so an unreadable store
    // refuses every write rather than replacing one entry. Saying otherwise would
    // promise a repair that never happens.
    return 'The local draft store on this device is unreadable, so no saved draft could be opened AND nothing can be saved here until it is repaired. Nothing was changed or removed. The editor is showing the generated report; use Copy Markdown or Download .md to keep your work.'
  }
  if (code === 'storage_unavailable') {
    return 'Local storage is unavailable in this browser, so no saved draft could be opened and nothing can be saved here. The editor is showing the generated report.'
  }
  return 'A saved draft for this report could not be opened on this device. The editor is showing the generated report instead.'
}

/**
 * The generated report moved on while a saved draft kept its own base.
 *
 * The saved base is retained deliberately: replacing edited text with a newer
 * generation would destroy work the reader did not agree to discard.
 */
export function staleSourceMessage(savedGeneratedAt: string): string {
  // The instruction has to hold whether or not a saved draft exists: a draft
  // that was never saved has no Delete to offer, and telling the reader to use
  // an unavailable control would read as a broken screen.
  return (
    'This draft was started from the report generated at '
    + savedGeneratedAt
    + '. A newer report has been generated since. Your text is kept as it is —'
    + ' nothing was replaced. To work from the newer report instead, first keep'
    + ' anything you want with Copy Markdown or Download .md, then discard this'
    + ' draft and open the editor again.'
  )
}
