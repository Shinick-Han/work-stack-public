import { z } from 'zod'
import {
  SYNC_STATES,
  TASK_PRIORITIES,
  TASK_STATUSES,
} from './types'
import { sha256 } from './schemaPrimitives'

export const syncStatusSchema = z.object({
  state: z.enum(SYNC_STATES),
  workspace_id: z.string().min(1).max(200),
  candidate_workspace_id: z.string().min(1).max(200),
  generation: z.number().int().nonnegative(),
  manifest_digest: z.string().regex(/^sha256:[0-9a-f]{64}$/).nullable(),
  changed_files: z.array(z.string().min(1).max(500)).max(500),
  reason: z.string().max(1_000).nullable(),
  rebind_available: z.boolean(),
}).strict()

export const workspaceRebindPreviewSchema = z.object({
  state: z.literal('workspace-identity-mismatch'),
  manifest_workspace_id: z.string().uuid(),
  candidate_workspace_id: z.string().uuid(),
  manifest_digest: z.string().regex(/^sha256:[0-9a-f]{64}$/),
  candidate_digest: z.string().regex(/^sha256:[0-9a-f]{64}$/),
  changed_files: z.array(z.string().min(1).max(500)).max(500),
}).strict()

export const workspaceRebindResultSchema = z.object({
  state: z.literal('in-sync'),
  workspace_id: z.string().uuid(),
  generation: z.number().int().nonnegative(),
  recovery_receipt_digest: z.string().regex(/^sha256:[0-9a-f]{64}$/),
  planning_mutated: z.literal(false),
}).strict()

const scopedIdentity = z
  .string()
  .min(1)
  .refine((value) => value.trim().length > 0, 'scoped identities must not be blank.')

export const taskSchema = z
  .object({
    id: z.string(),
    uid: z.string()
      .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
      .refine((value) => value !== '00000000-0000-0000-0000-000000000000', 'uid must not be nil.'),
    title: z.string(),
    detail: z.string().default(''),
    status: z.enum(TASK_STATUSES),
    priority: z.enum(TASK_PRIORITIES),
    due: z.string().nullable().default(null),
    scheduled: z.string().nullable().default(null),
    estimate_minutes: z.number().int().min(1).max(1440).nullable().default(null),
    tags: z.array(z.string()).default([]),
    objective_ids: z.array(z.string()).default([]),
    key_result_refs: z
      .array(
        z
          .object({
            objective_id: scopedIdentity,
            key_result_id: scopedIdentity,
          })
          .strict(),
      )
      .optional(),
    parent_id: z.string().nullable().default(null),
    dependencies: z.array(z.string()).default([]),
    subtasks: z
      .array(
        z
          .object({
            id: z.string(),
            title: z.string(),
            priority: z.enum(TASK_PRIORITIES).optional(),
            status: z.enum(TASK_STATUSES).optional(),
          })
          .passthrough(),
      )
      .default([]),
    notes: z
      .array(z.object({ date: z.string().optional(), text: z.string() }).passthrough())
      .default([]),
    created: z.string().optional(),
    updated_at: z.string().optional(),
    revision: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
    context_count: z.number().int().nonnegative().default(0),
  })
  .passthrough()

export const objectiveSchema = z
  .object({
    id: z.string(),
    objective: z.string(),
    title: z.string().optional(),
    quarter: z.string().optional(),
    status: z.string().optional(),
    key_results: z
      .array(
        z
          .object({
            id: z.string(),
            text: z.string(),
            target: z.string().optional(),
            progress: z.number().optional(),
            status: z.string().optional(),
          })
          .passthrough(),
      )
      .optional(),
    created: z.string().optional(),
    updated_at: z.string().optional(),
    revision: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER).default(0),
  })
  .passthrough()

export const objectiveDetailSchema = z.object({
  objective: objectiveSchema,
  tasks: z.array(taskSchema),
  activity: z.array(z.object({
    id: z.string(),
    type: z.string(),
    created_at: z.string(),
    details: z.record(z.string(), z.unknown()),
  }).passthrough()),
}).strict()

export const storageStatusSchema = z.object({
  workspace_id: z.string().uuid(),
  store_schema_version: z.number().int().positive(),
  product_version: z.string().min(1),
  remote_protocol_version: z.number().int().nonnegative(),
  file_count: z.number().int().positive(),
  total_bytes: z.number().int().nonnegative(),
  backup_format: z.literal('workstack-backup-v1'),
  restore_requires_shutdown: z.literal(true),
}).strict()

export const searchProjectionSchema = z.object({
  query: z.string(),
  items: z.array(z.object({
    kind: z.enum(['task', 'objective', 'note', 'capture', 'activity']),
    id: z.string(),
    title: z.string(),
    subtitle: z.string(),
    target_kind: z.enum(['task', 'objective', 'capture', 'workspace']),
    target_id: z.string().nullable(),
  }).strict()),
}).strict()

export const noteSchema = z
  .object({
    id: z.string(),
    text: z.string(),
    links: z.array(z.string()).default([]),
    created: z.string().optional(),
  })
  .passthrough()

export const workspaceSchema = z.object({
  schema_version: z.literal('1.0'),
  workspace: z.object({ id: z.string(), name: z.string() }),
  tasks: z.array(taskSchema),
  objectives: z.array(objectiveSchema),
  notes: z.array(noteSchema),
  edges: z.array(
    z.object({ source: z.string(), target: z.string(), kind: z.string() }).passthrough(),
  ),
  inbox_count: z.number().int().nonnegative().default(0),
})

export const worklogEntrySchema = z.object({
  task_id: z.string(),
  task: z.string(),
  done: z.array(z.string()),
  next: z.array(z.string()),
  blockers: z.array(z.string()),
  session_id: z.string().optional(),
  duration_seconds: z.number().int().nonnegative().optional(),
}).strict()

const weeklyReviewProjectSchema = worklogEntrySchema.extend({
  objective_ids: z.array(z.string()),
  dates: z.array(z.string()),
  duration_seconds: z.number().int().nonnegative(),
})

export const workSessionSchema = z.object({
  id: z.string().regex(/^WS-\d{6,}$/),
  task_id: z.string(),
  task: z.string(),
  date: z.string(),
  state: z.enum(['running', 'paused', 'stopped']),
  started_at: z.string().datetime({ offset: true }),
  updated_at: z.string().datetime({ offset: true }),
  elapsed_seconds: z.number().int().nonnegative(),
  worklog_state: z.enum(['not_ready', 'pending', 'recorded']),
}).strict()

export const workSessionProjectionSchema = z.object({
  current: workSessionSchema.nullable(),
  pending: z.array(workSessionSchema),
}).strict()

export const reviewProjectionSchema = z.object({
  day: z.object({
    date: z.string(),
    start_time: z.string().nullable(),
    entries: z.array(worklogEntrySchema),
  }).strict(),
  weekly: z.object({
    range: z.object({
      start: z.string(),
      end: z.string(),
      days: z.number().int().min(1).max(31),
    }).strict(),
    objectives: z.array(z.object({ id: z.string(), objective: z.string() }).strict()),
    projects: z.array(weeklyReviewProjectSchema),
  }).strict(),
}).strict()

const snapshotUuid = z.string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  .refine((value) => value !== '00000000-0000-0000-0000-000000000000', 'uid must not be nil.')

export const planningSnapshotSchema = z.object({
  detail: z.string(),
  due_date: z.string().nullable(),
  format: z.literal('workstack.planning-task-snapshot.v1'),
  legacy_task_id: z.string().regex(/^T-[0-9]{4,}$/),
  origin_ref: z.string(),
  planning_priority: z.enum(TASK_PRIORITIES),
  planning_status: z.enum(TASK_STATUSES),
  planning_task_uid: snapshotUuid,
  revision: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
  title: z.string(),
  workspace_uid: snapshotUuid,
}).strict()

export const snapshotPreviewSchema = z.object({
  snapshot: planningSnapshotSchema,
  digest: sha256,
  filename: z.string().regex(/^[0-9a-f-]{36}\.workstack-task\.json$/),
  omissions: z.tuple([
    z.literal('objectives'),
    z.literal('dependencies'),
    z.literal('subtasks'),
    z.literal('notes'),
    z.literal('tags'),
  ]),
}).strict()
