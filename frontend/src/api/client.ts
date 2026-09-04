import { z } from 'zod'
import {
  capturePacketSchema,
  checkpointAuditSchema,
  checkpointTransitionEventSchema,
  captureSchema,
  noteSchema,
  objectiveDetailSchema,
  objectiveSchema,
  replyCommandSchema,
  replyReceiptSchema,
  reviewProjectionSchema,
  searchProjectionSchema,
  snapshotPreviewSchema,
  storageStatusSchema,
  syncStatusSchema,
  workspaceRebindPreviewSchema,
  workspaceRebindResultSchema,
  taskDetailSchema,
  taskSchema,
  worklogEntrySchema,
  workSessionProjectionSchema,
  workSessionSchema,
  workspaceSchema,
} from '../domain/schemas'
import type {
  ApprovedReplyInput,
  CheckpointAudit,
  CheckpointTransitionEventRecord,
  CheckpointTransitionInput,
  Capture,
  CapturePacket,
  CaptureTaskInput,
  Note,
  Objective,
  ObjectiveDetail,
  QuickTaskInput,
  ReplyCommand,
  ReplyReceipt,
  ReviewEntryInput,
  ReviewProjection,
  SearchProjection,
  SnapshotPreview,
  StorageStatus,
  SyncStatus,
  WorkspaceRebindPreview,
  WorkspaceRebindResult,
  Task,
  TaskDetail,
  TaskPatch,
  WorkspaceProjection,
  WorklogEntry,
  WorkSession,
  WorkSessionEntryInput,
  WorkSessionProjection,
} from '../domain/types'
import {
  ApiError,
  CommitUnknownError,
  createIdempotencyKey,
  getData,
  mutateData,
  mutateIdempotent,
  type MutateReceipt,
} from './transport'
import { downloadBackup, downloadTaskSnapshot } from './downloads'

/** The keyed status receipt travels raw; the intent hook validates it. */
const rawReceiptSchema = z.unknown()

export interface TaskStatusIntentReceipt {
  status: number
  body: { data: unknown; meta: unknown }
}

/** One ambiguity message for both a failed and a contradictory D5 receipt. */
const AMBIGUOUS_TRANSITION =
  'The checkpoint transition may have committed. Retry the same request unchanged to verify it without duplication.'

export { ApiError, CommitUnknownError, createIdempotencyKey } from './transport'

const captureListSchema = z.union([
  z.array(captureSchema),
  z.object({ captures: z.array(captureSchema) }).transform(({ captures }) => captures),
])
const taskMutationSchema = z.union([
  taskSchema,
  z.object({ task: taskSchema }).transform(({ task }) => task),
])
const captureMutationSchema = z.union([
  captureSchema,
  z.object({ capture: captureSchema }).transform(({ capture }) => capture),
])
const replyMutationSchema = z.union([
  replyCommandSchema,
  z.object({ reply: replyCommandSchema }).strict().transform(({ reply }) => reply),
  z.object({ command: replyCommandSchema }).strict().transform(({ command }) => command),
])
const reviewCheckinSchema = z.object({ date: z.string(), start_time: z.string() }).strict()
const reviewEntryResponseSchema = worklogEntrySchema.extend({ date: z.string() })

async function capturePost<T>(path: string, body: unknown, schema: z.ZodType<T>): Promise<T> {
  const idempotencyKey = createIdempotencyKey()
  return mutateData(path, 'POST', body, schema, idempotencyKey)
}

const unknownResultSchema = z.unknown()

export const api = {
  getSyncStatus(): Promise<SyncStatus> {
    return getData('/api/v1/sync/status', syncStatusSchema)
  },

  getWorkspaceRebindPreview(): Promise<WorkspaceRebindPreview> {
    return getData('/api/v1/sync/rebind-preview', workspaceRebindPreviewSchema)
  },

  rebindWorkspace(preview: WorkspaceRebindPreview, idempotencyKey: string): Promise<WorkspaceRebindResult> {
    return mutateData(
      '/api/v1/sync/rebind-workspace',
      'POST',
      {
        confirmed: true,
        expected_manifest_workspace_id: preview.manifest_workspace_id,
        expected_candidate_workspace_id: preview.candidate_workspace_id,
        expected_manifest_digest: preview.manifest_digest,
        expected_candidate_digest: preview.candidate_digest,
      },
      workspaceRebindResultSchema,
      idempotencyKey,
      false,
    )
  },

  adoptSyncChanges(expectedGeneration: number, expectedManifestDigest: string, idempotencyKey: string): Promise<SyncStatus> {
    return mutateData(
      '/api/v1/sync/adopt',
      'POST',
      {
        expected_generation: expectedGeneration,
        expected_manifest_digest: expectedManifestDigest,
      },
      syncStatusSchema,
      idempotencyKey,
    )
  },

  getWorkspace(): Promise<WorkspaceProjection> {
    return getData('/api/v1/workspace', workspaceSchema)
  },

  getStorageStatus(): Promise<StorageStatus> {
    return getData('/api/v1/storage', storageStatusSchema)
  },

  downloadBackup,

  search(query: string, limit = 30): Promise<SearchProjection> {
    const parameters = new URLSearchParams({ q: query, limit: String(limit) })
    return getData(`/api/v1/search?${parameters}`, searchProjectionSchema)
  },

  /** Whole-workspace audit. UI day filtering happens only after validation. */
  getCheckpointAudit(): Promise<CheckpointAudit> {
    return getData('/api/v1/review/checkpoints', checkpointAuditSchema)
  },

  /**
   * One attempt through the unchanged idempotent transport. The explanation is
   * sent verbatim so the server's normalization stays authoritative.
   */
  transitionCheckpoint(
    checkpointId: string,
    input: CheckpointTransitionInput,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<CheckpointTransitionEventRecord> {
    const receipt: MutateReceipt = {}
    return mutateIdempotent(
      `/api/v1/review/checkpoints/${encodeURIComponent(checkpointId)}/transitions`,
      input,
      checkpointTransitionEventSchema,
      idempotencyKey,
      AMBIGUOUS_TRANSITION,
      // Exactly one POST per explicit submit or explicit same-snapshot retry.
      { singleAttempt: true, receipt },
    ).then((record) => {
      // Only a fresh 201 or a replayed 200 whose receipt names the requested
      // checkpoint, state and next revision establishes that this committed.
      const replayed = (receipt.meta as { replayed?: unknown } | undefined)?.replayed
      const routeAccepted = (receipt.status === 201 && replayed === false)
        || (receipt.status === 200 && replayed === true)
      const matches = routeAccepted
        && record.checkpoint_id === checkpointId
        && record.state === input.state
        && record.revision === input.revision + 1
      if (!matches) {
        throw new CommitUnknownError(AMBIGUOUS_TRANSITION, {
          expected: { checkpointId, state: input.state, revision: input.revision + 1 },
          received: record,
          status: receipt.status,
          meta: receipt.meta,
        })
      }
      return record
    })
  },

  getReview(date: string, days = 7): Promise<ReviewProjection> {
    const query = new URLSearchParams({ date, days: String(days) })
    return getData(`/api/v1/review?${query}`, reviewProjectionSchema)
  },

  checkinReview(
    date: string,
    time: string,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<{ date: string; start_time: string }> {
    return mutateIdempotent(
      '/api/v1/review/checkin',
      { date, time },
      reviewCheckinSchema,
      idempotencyKey,
      'The check-in may have committed. Retry unchanged to verify it without duplication.',
    )
  },

  addReviewEntry(
    input: ReviewEntryInput,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<WorklogEntry & { date: string }> {
    return mutateIdempotent(
      '/api/v1/review/entries',
      input,
      reviewEntryResponseSchema,
      idempotencyKey,
      'The review entry may have committed. Retry unchanged to verify it without duplication.',
    )
  },

  getWorkSessions(): Promise<WorkSessionProjection> {
    return getData('/api/v1/work-sessions', workSessionProjectionSchema)
  },

  startWorkSession(
    taskId: string,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<WorkSession> {
    return mutateIdempotent(
      '/api/v1/work-sessions',
      { task_id: taskId },
      workSessionSchema,
      idempotencyKey,
      'The work session may have started. Retry unchanged to verify it without duplication.',
    )
  },

  transitionWorkSession(
    sessionId: string,
    action: 'pause' | 'resume' | 'stop',
    idempotencyKey = createIdempotencyKey(),
  ): Promise<WorkSession> {
    return mutateIdempotent(
      `/api/v1/work-sessions/${encodeURIComponent(sessionId)}/${action}`,
      {},
      workSessionSchema,
      idempotencyKey,
      `The work session may have been ${action === 'pause' ? 'paused' : action === 'resume' ? 'resumed' : 'stopped'}. Retry unchanged to verify it.`,
    )
  },

  recordWorkSession(
    sessionId: string,
    input: WorkSessionEntryInput,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<WorklogEntry & { date: string }> {
    return mutateIdempotent(
      `/api/v1/work-sessions/${encodeURIComponent(sessionId)}/worklog`,
      input,
      reviewEntryResponseSchema,
      idempotencyKey,
      'The worklog may have committed. Retry unchanged to verify it without duplication.',
    )
  },

  getTask(taskId: string): Promise<TaskDetail> {
    return getData(`/api/v1/tasks/${encodeURIComponent(taskId)}`, taskDetailSchema) as Promise<TaskDetail>
  },

  getTaskSnapshot(taskId: string): Promise<SnapshotPreview> {
    return getData(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/snapshot`,
      snapshotPreviewSchema,
    )
  },

  downloadTaskSnapshot,

  /**
   * The keyed Task-status branch of the existing PATCH route.
   *
   * The caller ALWAYS supplies the key - none is generated here - and the
   * answered HTTP status and the raw `{data, meta}` are handed back exactly as
   * received. Nothing is unwrapped, defaulted or parsed into a Task: the
   * admitted intent hook owns receipt validation. Early planning publication is
   * suppressed so no returned Task is installed before that classification.
   */
  patchTaskStatusIntent(
    taskId: string,
    body: { status: string; revision: number },
    idempotencyKey: string,
  ): Promise<TaskStatusIntentReceipt> {
    const receipt: MutateReceipt = {}
    return mutateData(
      `/api/v1/tasks/${encodeURIComponent(taskId)}`,
      'PATCH',
      body,
      rawReceiptSchema,
      idempotencyKey,
      false,
      { singleAttempt: true, receipt },
    ).then(
      (data) => ({ status: receipt.status ?? 0, body: { data, meta: receipt.meta } }),
      (error) => {
        // A determinate conflict is an ANSWER, not a lost request: it must reach
        // the intent hook's 409 branch instead of being read as network loss.
        if (error instanceof ApiError && error.status === 409) {
          return { status: 409, body: { data: undefined, meta: receipt.meta } }
        }
        throw error
      },
    )
  },

  patchTask(taskId: string, patch: TaskPatch): Promise<Task> {
    return mutateData(
      `/api/v1/tasks/${encodeURIComponent(taskId)}`,
      'PATCH',
      patch,
      taskMutationSchema,
    )
  },

  addTaskNote(
    taskId: string,
    text: string,
    revision: number,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<Task> {
    return mutateIdempotent(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/notes`,
      { text, revision },
      taskMutationSchema,
      idempotencyKey,
      'The Task log entry may have committed. Retry the unchanged entry to verify it without duplication.',
    )
  },

  addSubtask(
    taskId: string,
    title: string,
    priority: Task['priority'],
    revision: number,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<Task> {
    return mutateIdempotent(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/subtasks`,
      { title, priority, revision },
      taskMutationSchema,
      idempotencyKey,
      'The Step may have committed. Retry the unchanged Step to verify it without duplication.',
    )
  },

  setSubtaskStatus(
    taskId: string,
    subtaskId: string,
    status: Task['status'],
    revision: number,
  ): Promise<Task> {
    return mutateData(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/subtasks/${encodeURIComponent(subtaskId)}`,
      'PATCH',
      { status, revision },
      taskMutationSchema,
    )
  },

  createObjective(
    objective: string,
    quarter: string,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<Objective> {
    return mutateIdempotent(
      '/api/v1/objectives',
      { objective, quarter },
      objectiveSchema,
      idempotencyKey,
      'The Objective may have committed. Retry the unchanged Objective to verify it without duplication.',
    )
  },

  getObjective(objectiveId: string): Promise<ObjectiveDetail> {
    return getData(
      `/api/v1/objectives/${encodeURIComponent(objectiveId)}`,
      objectiveDetailSchema,
    )
  },

  addKeyResult(
    objectiveId: string,
    text: string,
    target: string,
    revision: number,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<Objective> {
    return mutateIdempotent(
      `/api/v1/objectives/${encodeURIComponent(objectiveId)}/key-results`,
      { text, target, revision },
      objectiveSchema,
      idempotencyKey,
      'The Key Result may have committed. Retry the unchanged Key Result to verify it without duplication.',
    )
  },

  patchObjective(
    objectiveId: string,
    fields: { objective?: string; quarter?: string; status?: string },
    revision: number,
  ): Promise<Objective> {
    return mutateData(
      `/api/v1/objectives/${encodeURIComponent(objectiveId)}`,
      'PATCH',
      { ...fields, revision },
      objectiveSchema,
    )
  },

  patchKeyResult(
    objectiveId: string,
    keyResultId: string,
    fields: { text?: string; target?: string; progress?: number; status?: string },
    revision: number,
  ): Promise<Objective> {
    return mutateData(
      `/api/v1/objectives/${encodeURIComponent(objectiveId)}/key-results/${encodeURIComponent(keyResultId)}`,
      'PATCH',
      { ...fields, revision },
      objectiveSchema,
    )
  },

  createNote(
    text: string,
    links: string[],
    idempotencyKey = createIdempotencyKey(),
  ): Promise<Note> {
    return mutateIdempotent(
      '/api/v1/notes',
      { text, links },
      noteSchema,
      idempotencyKey,
      'The Context card may have committed. Retry the unchanged card to verify it without duplication.',
    )
  },

  async createTask(input: QuickTaskInput): Promise<Task> {
    const idempotencyKey = createIdempotencyKey()
    try {
      return await mutateData(
        '/api/v1/tasks',
        'POST',
        input,
        taskMutationSchema,
        idempotencyKey,
      )
    } catch (error) {
      if (error instanceof ApiError) throw error
      throw new CommitUnknownError(
        'Task creation may have committed, but the server response could not be verified.',
        error,
      )
    }
  },

  getCaptures(status: string = 'all'): Promise<Capture[]> {
    const query = new URLSearchParams({ status })
    return getData(`/api/v1/captures?${query}`, captureListSchema)
  },

  ingestCapture(packet: CapturePacket): Promise<Capture> {
    return capturePost('/api/v1/captures', capturePacketSchema.parse(packet), captureMutationSchema)
  },

  linkCapture(captureId: string, taskId: string): Promise<unknown> {
    return capturePost(
      `/api/v1/captures/${encodeURIComponent(captureId)}/link`,
      { task_id: taskId },
      unknownResultSchema,
    )
  },

  convertCaptureAction(
    captureId: string,
    actionId: string,
    objectiveIds: string[] = [],
  ): Promise<unknown> {
    return capturePost(
      `/api/v1/captures/${encodeURIComponent(captureId)}/actions/${encodeURIComponent(actionId)}/task`,
      { objective_ids: objectiveIds },
      unknownResultSchema,
    )
  },

  createTaskFromCapture(
    captureId: string,
    input: CaptureTaskInput,
    idempotencyKey = createIdempotencyKey(),
  ): Promise<Task> {
    return mutateIdempotent(
      `/api/v1/captures/${encodeURIComponent(captureId)}/task`,
      input,
      taskMutationSchema,
      idempotencyKey,
      'The source Task may have committed. Retry the unchanged draft to verify it without duplication.',
    )
  },

  createReply(input: ApprovedReplyInput): Promise<ReplyCommand> {
    return capturePost('/api/v1/replies', input, replyMutationSchema)
  },

  importReplyReceipt(replyId: string, receipt: ReplyReceipt): Promise<ReplyCommand> {
    return capturePost(
      `/api/v1/replies/${encodeURIComponent(replyId)}/receipt`,
      replyReceiptSchema.parse(receipt),
      replyMutationSchema,
    )
  },

  dismissCapture(captureId: string): Promise<unknown> {
    return capturePost(
      `/api/v1/captures/${encodeURIComponent(captureId)}/dismiss`,
      {},
      unknownResultSchema,
    )
  },
}
