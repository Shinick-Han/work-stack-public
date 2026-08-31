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
  taskDetailSchema,
  taskSchema,
  worklogEntrySchema,
  workSessionProjectionSchema,
  workSessionSchema,
  workspaceSchema,
} from '../domain/schemas'
import type {
  ApprovedReplyInput,
  BackupDownload,
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
  SnapshotDownload,
  SnapshotPreview,
  StorageStatus,
  SyncStatus,
  Task,
  TaskDetail,
  TaskPatch,
  WorkspaceProjection,
  WorklogEntry,
  WorkSession,
  WorkSessionEntryInput,
  WorkSessionProjection,
} from '../domain/types'
import { publishPlanningChange } from '../app/crossTabSync'

const sessionSchema = z.object({ csrf_token: z.string().min(8) })
const envelopeSchema = z.object({ data: z.unknown(), meta: z.record(z.string(), z.unknown()).optional() })
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

interface ErrorEnvelope {
  error?: { code?: string; message?: string; details?: unknown }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

export class CommitUnknownError extends Error {
  readonly cause: unknown

  constructor(message: string, cause: unknown) {
    super(message)
    this.name = 'CommitUnknownError'
    this.cause = cause
  }
}

let csrfTokenPromise: Promise<string> | null = null

async function fetchWithNetworkRetry(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init)
  } catch (firstError) {
    if (init.signal?.aborted) throw firstError
    // The exact same init object is retried. For idempotent mutations this preserves the
    // Idempotency-Key generated for the logical operation.
    return fetch(input, init)
  }
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    throw new ApiError(response.status, 'invalid_json', 'The server returned an unreadable response.')
  }
}

async function assertOk(response: Response): Promise<unknown> {
  const payload = await readJson(response)
  if (response.ok) return payload
  const candidate = payload as ErrorEnvelope | null
  throw new ApiError(
    response.status,
    candidate?.error?.code ?? 'request_failed',
    candidate?.error?.message ?? `Request failed with status ${response.status}.`,
    candidate?.error?.details,
  )
}

async function getCsrfToken(force = false): Promise<string> {
  if (force) csrfTokenPromise = null
  csrfTokenPromise ??= (async () => {
    const response = await fetchWithNetworkRetry('/api/v1/session', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    })
    const payload = envelopeSchema.parse(await assertOk(response))
    return sessionSchema.parse(payload.data).csrf_token
  })()
  return csrfTokenPromise
}

async function getData<T>(path: string, schema: z.ZodType<T>): Promise<T> {
  const response = await fetchWithNetworkRetry(path, {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Accept: 'application/json' },
  })
  const envelope = envelopeSchema.parse(await assertOk(response))
  return schema.parse(envelope.data)
}

async function mutateData<T>(
  path: string,
  method: 'POST' | 'PATCH',
  body: unknown,
  schema: z.ZodType<T>,
  idempotencyKey?: string,
): Promise<T> {
  const encodedBody = JSON.stringify(body)
  const makeHeaders = async (refreshSession = false) => {
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-WorkStack-CSRF': await getCsrfToken(refreshSession),
    }
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
    return headers
  }

  const request = idempotencyKey ? fetchWithNetworkRetry : fetch
  let response = await request(path, {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    headers: await makeHeaders(),
    body: encodedBody,
  })

  // A restarted local server rotates the CSRF nonce. Retry once with the same body and,
  // critically, the same idempotency key.
  if (response.status === 403) {
    response = await request(path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: await makeHeaders(true),
      body: encodedBody,
    })
  }

  const envelope = envelopeSchema.parse(await assertOk(response))
  const parsed = schema.parse(envelope.data)
  publishPlanningChange()
  return parsed
}

export function createIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return `workstack:${crypto.randomUUID()}`
  }
  return `workstack:${Date.now()}:${Math.random().toString(36).slice(2, 14)}`
}

async function capturePost<T>(path: string, body: unknown, schema: z.ZodType<T>): Promise<T> {
  const idempotencyKey = createIdempotencyKey()
  return mutateData(path, 'POST', body, schema, idempotencyKey)
}

async function mutateIdempotent<T>(
  path: string,
  body: unknown,
  schema: z.ZodType<T>,
  idempotencyKey: string,
  commitUnknownMessage: string,
): Promise<T> {
  try {
    return await mutateData(path, 'POST', body, schema, idempotencyKey)
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new CommitUnknownError(commitUnknownMessage, error)
  }
}

const unknownResultSchema = z.unknown()

export const api = {
  getSyncStatus(): Promise<SyncStatus> {
    return getData('/api/v1/sync/status', syncStatusSchema)
  },

  adoptSyncChanges(expectedGeneration: number, expectedManifestDigest: string): Promise<SyncStatus> {
    return mutateData(
      '/api/v1/sync/adopt',
      'POST',
      {
        expected_generation: expectedGeneration,
        expected_manifest_digest: expectedManifestDigest,
      },
      syncStatusSchema,
    )
  },

  getWorkspace(): Promise<WorkspaceProjection> {
    return getData('/api/v1/workspace', workspaceSchema)
  },

  getStorageStatus(): Promise<StorageStatus> {
    return getData('/api/v1/storage', storageStatusSchema)
  },

  async downloadBackup(expectedWorkspaceId: string): Promise<BackupDownload> {
    const encodedBody = JSON.stringify({ confirmed: true })
    const makeHeaders = async (refreshSession = false) => ({
      Accept: 'application/zip',
      'Content-Type': 'application/json',
      'X-WorkStack-CSRF': await getCsrfToken(refreshSession),
    })
    let response = await fetchWithNetworkRetry('/api/v1/maintenance/backup', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: await makeHeaders(),
      body: encodedBody,
    })
    if (response.status === 403) {
      response = await fetchWithNetworkRetry('/api/v1/maintenance/backup', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: await makeHeaders(true),
        body: encodedBody,
      })
    }
    if (!response.ok) {
      await assertOk(response)
      throw new ApiError(response.status, 'backup_download_failed', 'Backup download failed.')
    }
    const digest = response.headers.get('X-WorkStack-Backup-Digest')
    const workspaceId = response.headers.get('X-WorkStack-Workspace-Id')
    const contentType = response.headers.get('Content-Type')
    const disposition = response.headers.get('Content-Disposition') ?? ''
    const filename = /^attachment; filename="(workstack-backup-[0-9TZ]+-[0-9a-f]{8}\.zip)"$/.exec(disposition)?.[1]
    if (contentType !== 'application/zip' || !digest?.match(/^sha256:[0-9a-f]{64}$/) || workspaceId !== expectedWorkspaceId || !filename) {
      throw new ApiError(
        response.status,
        'backup_response_invalid',
        'The backup response could not be verified.',
      )
    }
    return { blob: await response.blob(), digest, filename }
  },

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

  async downloadTaskSnapshot(
    taskId: string,
    expectedRevision: number,
    expectedDigest: string,
  ): Promise<SnapshotDownload> {
    const path = `/api/v1/tasks/${encodeURIComponent(taskId)}/snapshot/export`
    const encodedBody = JSON.stringify({
      disclosure_confirmed: true,
      expected_digest: expectedDigest,
      expected_revision: expectedRevision,
    })
    const makeHeaders = async (refreshSession = false) => ({
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-WorkStack-CSRF': await getCsrfToken(refreshSession),
    })
    let response = await fetchWithNetworkRetry(path, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: await makeHeaders(),
      body: encodedBody,
    })
    if (response.status === 403) {
      response = await fetchWithNetworkRetry(path, {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: await makeHeaders(true),
        body: encodedBody,
      })
    }
    if (!response.ok) {
      await assertOk(response)
      throw new ApiError(response.status, 'snapshot_export_failed', 'Snapshot export failed.')
    }
    const digest = response.headers.get('X-WorkStack-Snapshot-Digest')
    const disposition = response.headers.get('Content-Disposition') ?? ''
    const filename = /^attachment; filename="([0-9a-f-]{36}\.workstack-task\.json)"$/.exec(disposition)?.[1]
    if (digest !== expectedDigest || !filename) {
      throw new ApiError(
        response.status,
        'snapshot_response_invalid',
        'The snapshot download response could not be verified.',
      )
    }
    return { blob: await response.blob(), digest, filename }
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

  createTaskFromCapture(captureId: string, input: CaptureTaskInput): Promise<Task> {
    return capturePost(
      `/api/v1/captures/${encodeURIComponent(captureId)}/task`,
      input,
      taskMutationSchema,
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
