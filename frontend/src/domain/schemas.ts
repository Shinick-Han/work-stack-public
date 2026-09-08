import { z } from 'zod'
import { noteSchema, taskSchema } from './schemaPlanning'
import { captureSchema, replyCommandSchema } from './schemaSourceCaptureReply'

export {
  objectiveDetailSchema,
  objectiveSchema,
  noteSchema,
  planningSnapshotSchema,
  reviewProjectionSchema,
  searchProjectionSchema,
  snapshotPreviewSchema,
  storageStatusSchema,
  syncStatusSchema,
  taskSchema,
  workSessionProjectionSchema,
  workSessionSchema,
  worklogEntrySchema,
  workspaceRebindPreviewSchema,
  workspaceRebindResultSchema,
  workspaceSchema,
} from './schemaPlanning'

export {
  capturePacketSchema,
  captureSchema,
  findForbiddenCaptureKey,
  oobRequestSchema,
  replyCommandSchema,
  replyReceiptSchema,
  replyTargetSchema,
} from './schemaSourceCaptureReply'

export {
  checkpointAuditEntrySchema,
  checkpointAuditSchema,
  checkpointTransitionEventSchema,
} from './schemaCheckpoint'

const contextMetadataSchema = z.object({
  ref: z.object({ kind: z.enum(['note', 'capture']), id: z.string().min(1) }).strict(),
  connections: z.array(z.object({
    target: z.object({ kind: z.enum(['task', 'objective']), id: z.string().min(1) }).strict(),
    reasons: z.array(z.enum(['note-link', 'capture-link', 'capture-conversion'])).min(1),
  }).strict()),
  date_precision: z.enum(['date', 'instant', 'unknown']),
}).strict()

// Legacy context records remain readable, but partial or malformed new metadata
// cannot fall through a permissive legacy union branch.
const contextItemSchema = z.record(z.string(), z.unknown()).superRefine((item, context) => {
  if (!['ref', 'connections', 'date_precision'].some((field) => field in item)) return
  const { ref, connections, date_precision, ...original } = item
  const metadata = contextMetadataSchema.safeParse({ ref, connections, date_precision })
  if (!metadata.success) {
    context.addIssue({ code: 'custom', message: 'Invalid shared context metadata' })
    return
  }
  if (metadata.data.ref.id !== item.id) {
    context.addIssue({ code: 'custom', path: ['ref', 'id'], message: 'Context reference must match its original id' })
  }
  const kind = metadata.data.ref.kind
  const record = (kind === 'capture' ? captureSchema : noteSchema).safeParse(original)
  if (!record.success) {
    context.addIssue({ code: 'custom', message: 'Invalid original shared context record' })
  }
  for (const connection of metadata.data.connections) {
    const allowed = kind === 'note'
      ? connection.reasons.every((reason) => reason === 'note-link')
      : connection.target.kind === 'task' && connection.reasons.every((reason) => reason !== 'note-link')
    if (!allowed) context.addIssue({ code: 'custom', path: ['connections'], message: 'Context connection reason does not match its source' })
  }
})

export const taskDetailSchema = z.object({
  task: taskSchema,
  context: z.array(contextItemSchema),
  activity: z.array(z.record(z.string(), z.unknown())),
  replies: z.array(replyCommandSchema).default([]),
}).strict()
