/**
 * Reader-facing copy for saved-report history. Codes stay off the screen.
 *
 * Finalize is a Work Stack write only: it never sends a report elsewhere.
 */

export const EMPTY_SAVED_REPORTS =
  'No saved reports in this workspace yet. Save a local daily draft from the daily report editor first. This list does not create or edit report history.'

export const FINALIZE_DISCLAIMER =
  'Finalize stores a finished report inside Work Stack. It does not send the report anywhere else.'

export const UNSETTLED_TRANSITION =
  'Work Stack did not confirm this change, so the report may or may not have updated. The last confirmed state is still shown. Ask the same question again to settle it without sending a different request.'

export const RECONCILE_TRANSITION =
  'This report no longer matches the revision last confirmed here. Refresh this report before making another change. The text already on screen was not replaced.'

export const CURSOR_INVALID_MESSAGE =
  'This list page marker is no longer valid for the current filter and workspace. Refresh the list to continue. Reports already on screen are unchanged.'

export const SOURCE_STALE_MESSAGE =
  'The day this report was based on has changed since it was saved. The saved text is unchanged.'

export const INERT_HISTORY_HINT = 'Authored text is shown exactly as saved. It is never turned into HTML.'

const REFUSALS: Record<string, string> = {
  idempotency_conflict:
    'This change could not be matched to the request that was sent, so nothing new was written.',
  invalid_idempotency_key:
    'This change was not accepted, so nothing was written.',
  report_body_invalid:
    'The workspace refused this request because it falls outside the bounds it accepts, so nothing was written.',
  report_capability_unavailable:
    'This workspace cannot hold saved reports, so nothing was written.',
  report_cursor_invalid: CURSOR_INVALID_MESSAGE,
  report_duplicate_period:
    'Another saved report already covers this day, so restore did not run. Archive that newer report first, then restore.',
  report_idempotency_capacity:
    'The workspace could not take another change just now, so nothing was written.',
  report_not_found:
    'That saved report is not in this workspace.',
  report_revision_conflict: RECONCILE_TRANSITION,
  report_revision_limit:
    'This report cannot take another authored version, so nothing was written. Finalize, archive, and restore still use the last confirmed text.',
  report_source_changed:
    'The day this report was based on is no longer the current one, so nothing was written. Refresh this report. The text already on screen was not replaced.',
  report_state_invalid:
    'That change is not allowed from the last confirmed status, so nothing was written.',
  report_storage_full:
    'There is no room left in this workspace for this change, so nothing was written.',
  report_template_unsupported:
    'Only daily reports can be stored in this workspace, so nothing was written.',
  store_sync_required:
    'This workspace is not in sync, so nothing was written. Bring it back in sync, then try again.',
  workspace_mismatch:
    'This report belongs to a different workspace than the one open here, so nothing was written.',
}

export function savedReportsRefusalMessage(code: string): string {
  return REFUSALS[code] ?? 'This saved report could not be updated, so nothing was written.'
}

export function statusLabel(state: 'draft' | 'finalized' | 'archived'): string {
  if (state === 'finalized') return 'Finalized'
  if (state === 'archived') return 'Archived'
  return 'Draft'
}

export function filterLabel(filter: 'active' | 'archived' | 'all'): string {
  if (filter === 'archived') return 'Archived'
  if (filter === 'all') return 'All'
  return 'Active'
}
