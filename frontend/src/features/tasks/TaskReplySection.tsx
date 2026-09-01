import { Button, Pill } from '../../components/Primitives'
import type { ApprovedReplyInput, ReplyCommand, ReplyReceipt } from '../../domain/types'
import { ReplyComposer, type ReplySource } from './ReplyComposer'

interface TaskReplySectionProps {
  onCreate: (input: ApprovedReplyInput) => Promise<ReplyCommand>
  onImportReceipt: (replyId: string, receipt: ReplyReceipt) => Promise<ReplyCommand>
  onToggle: () => void
  open: boolean
  replies: ReplyCommand[]
  sources: ReplySource[]
  taskId: string
  unavailableSources: ReplySource[]
}

export function TaskReplySection({
  onCreate,
  onImportReceipt,
  onToggle,
  open,
  replies,
  sources,
  taskId,
  unavailableSources,
}: TaskReplySectionProps) {
  if (!sources.length && !unavailableSources.length) return null

  return (
    <>
      {sources.length ? (
        <section className="reply-launch">
          <div><strong>Reply to a linked Microsoft thread</strong><p>The target comes only from the selected Capture. Approval creates a command; sending remains a manual agent handoff.</p></div>
          {replies.length ? <div className="reply-launch__states">{replies.map((reply) => <Pill key={reply.id} tone={reply.state}>{reply.state}</Pill>)}</div> : null}
          <Button icon="command" onClick={onToggle} variant={open ? 'ghost' : 'primary'}>{open ? 'Close reply composer' : 'Prepare Outlook/Teams reply'}</Button>
        </section>
      ) : null}
      {unavailableSources.length ? (
        <section className="reply-launch reply-launch--unavailable">
          <div><strong>Microsoft reply unavailable</strong><p>{unavailableSources.map((source) => source.display_title).join(', ')} cannot be used for replies until that provider’s read and reply capabilities pass Gate 0.</p></div>
          <Pill tone="neutral">Reply unavailable · Gate 0 pending</Pill>
        </section>
      ) : null}
      {open && sources.length ? (
        <ReplyComposer
          onCreate={onCreate}
          onImportReceipt={onImportReceipt}
          replies={replies}
          sources={sources}
          taskId={taskId}
        />
      ) : null}
    </>
  )
}
