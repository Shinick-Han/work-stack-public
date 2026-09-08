import type { MicrosoftProviderGates } from '../../config/providerGates'
import type { ApprovedReplyInput, ReplyCommand, ReplyReceipt, Task, TaskDetail } from '../../domain/types'
import { TaskKnowledgePanel } from '../knowledge/TaskKnowledgePanel'
import { TaskContextTimeline } from './TaskDrawerTimelines'
import { TaskReplySection } from './TaskReplySection'
import { toResumeProgressFacts } from './taskResumeProgressAdapter'
import type { TaskDrawerSelection } from './taskDrawerSelectors'
import type { TaskResumeFactsResult } from './useTaskResumeFacts'

export function TaskContextSubview({
  context,
  facts,
  onBack,
  onCreate,
  onImportReceipt,
  onToggle,
  open,
  providerGates,
  replies,
  selection,
  task,
  taskId,
  workspaceUid,
}: {
  context: TaskDetail['context']
  facts: TaskResumeFactsResult
  onBack: () => void
  onCreate: (input: ApprovedReplyInput) => Promise<ReplyCommand>
  onImportReceipt: (replyId: string, receipt: ReplyReceipt) => Promise<ReplyCommand>
  onToggle: () => void
  open: boolean
  providerGates: MicrosoftProviderGates
  replies: ReplyCommand[]
  selection: TaskDrawerSelection
  task: Task
  taskId: string
  workspaceUid: string
}) {
  // Leaving is state only. Focus is returned by the drawer chrome once React has
  // committed the surface change and the control to return to is mounted again.
  return (
    <div className="task-context-subview">
      <TaskKnowledgePanel
        onBack={onBack}
        progress={toResumeProgressFacts(facts)}
        task={task}
        workspaceUid={workspaceUid}
      />
      <TaskReplySection
        onCreate={onCreate}
        onImportReceipt={onImportReceipt}
        onToggle={onToggle}
        open={open}
        replies={replies}
        sources={selection.replySources}
        taskId={taskId}
        unavailableSources={selection.replyUnavailableSources}
      />
      <TaskContextTimeline context={context} providerGates={providerGates} />
    </div>
  )
}
