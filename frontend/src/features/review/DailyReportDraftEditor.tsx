import { Dialog } from '../../components/Dialog'
import { LOCAL_ONLY_LABEL } from './reportDraftMessages'
import { ReportPromotionPanel } from './ReportPromotionPanel'
import { DraftBody, DraftFooter, DraftPrompts } from './reportDraftEditorViews'
import { useReportDraftEditor } from './useReportDraftEditor'
import type { DailyReportDraftSource } from './reportDraftEditorModel'
import type { ReportDraftCoordinate, ReportDraftStore } from './reportDraftStorage'
import './dailyReportDraftEditor.css'

/**
 * Standalone editor for the origin-local edited daily-report draft.
 *
 * It edits ONE buffer on ONE device. It never finalizes or publishes, never
 * generates text and never calls a model. Saving the local draft is always an
 * explicit click with a compare-and-set against the revision this editor loaded,
 * so a draft written in another tab is reported rather than overwritten.
 *
 * Saving the same text to the workspace is a SEPARATE explicit action, offered by
 * `ReportPromotionPanel`. It creates a workspace report and leaves the local
 * draft, the text on screen and both fallbacks exactly as they are, whether it
 * succeeds or fails.
 *
 * The rule the whole feature is built around: the reader's text is never lost
 * without them saying so. A failed save keeps the text and names the fallback; a
 * newer generated report never replaces edited text; deleting the saved copy is a
 * different act from discarding the text on screen; and closing while anything is
 * unsaved asks first. Copy Markdown and Download .md work whatever storage does.
 *
 * This file is the composition only. The state machine lives in
 * `reportDraftEditorModel`, the lifecycle and storage calls in
 * `useReportDraftEditor`, and the markup in `reportDraftEditorViews`.
 */

export type { DailyReportDraftSource } from './reportDraftEditorModel'

export interface DailyReportDraftEditorProps {
  coordinate: ReportDraftCoordinate
  source: DailyReportDraftSource
  onClose: () => void
  /**
   * Optional injected store. Tests pass a deterministic fake; production leaves
   * it out and gets the browser-backed store.
   */
  store?: ReportDraftStore
}

export function DailyReportDraftEditor({
  coordinate,
  onClose,
  source,
  store,
}: DailyReportDraftEditorProps) {
  const model = useReportDraftEditor({ coordinate, onClose, source, store })
  return (
    <Dialog
      open
      onClose={model.actions.requestClose}
      size="large"
      title="Edit local report draft"
      description={LOCAL_ONLY_LABEL + ' Saving it to your workspace is a separate action below.'}
      footer={<DraftFooter model={model} />}
    >
      <DraftPrompts model={model} />
      <DraftBody model={model} source={source} />
      <ReportPromotionPanel model={model} source={source} />
    </Dialog>
  )
}
