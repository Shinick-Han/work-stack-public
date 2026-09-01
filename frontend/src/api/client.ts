import { z } from 'zod'
import {
  capturePacketSchema,
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
} from './transport'
import { downloadBackup, downloadTaskSnapshot } from './downloads'

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
