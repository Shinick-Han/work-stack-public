import { Button } from '../../components/Primitives'
import type { KnowledgeImportEnvelope } from '../../domain/knowledgeImport'
import { KnowledgeRequestLauncher } from './KnowledgeRequestLauncher'

export function InboxHeadingActions({
  microsoftReadAvailable,
  onCopyMicrosoftRequest,
  onImport,
  onImportAgentResult,
  onReviewKnowledge,
  workspaceUid,
}: {
  microsoftReadAvailable: boolean
  onCopyMicrosoftRequest: () => void
  onImport: () => void
  onImportAgentResult: () => void
  onReviewKnowledge?: (envelope: KnowledgeImportEnvelope) => void
  workspaceUid: string
}) {
  return (
    <div className="page-heading__actions inbox-heading-actions">
      {/* The knowledge request is an owner-policy flow, not a Microsoft one: it is
          offered whatever the provider gates say, and it reads its own scope. */}
      <KnowledgeRequestLauncher
        onReviewKnowledge={onReviewKnowledge}
        workspaceUid={workspaceUid}
      />
      {microsoftReadAvailable ? <Button icon="command" onClick={onCopyMicrosoftRequest} variant="primary">Copy Microsoft 365 request</Button> : null}
      {microsoftReadAvailable ? <Button icon="upload" onClick={onImportAgentResult}>Import agent result</Button> : null}
      <Button icon="upload" onClick={onImport} variant="ghost">Import packet</Button>
    </div>
  )
}
