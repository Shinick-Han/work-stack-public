import { z } from 'zod'
import {
  CAPTURE_STATUSES,
  MICROSOFT_PROVIDERS,
  REPLY_OUTCOMES,
  REPLY_STATES,
  TASK_PRIORITIES,
  TASK_STATUSES,
  SYNC_STATES,
  type CapturePacket,
  type OobRequest,
  type ReplyCommand,
  type ReplyReceipt,
} from './types'

export const syncStatusSchema = z.object({
  state: z.enum(SYNC_STATES),
  workspace_id: z.string().min(1).max(200),
  generation: z.number().int().nonnegative(),
  manifest_digest: z.string().regex(/^sha256:[0-9a-f]{64}$/).nullable(),
  changed_files: z.array(z.string().min(1).max(500)).max(500),
  reason: z.string().max(1_000).nullable(),
}).strict()

const sha256 = z.string().regex(/^sha256:[0-9a-f]{64}$/, 'Expected sha256: followed by 64 lowercase hex characters')
const safeExternalUrl = z.string().url().refine(
  (value) => new URL(value).protocol === 'https:',
  'Source links must use HTTPS.',
)

const urlCredentialNames = new Set([
  'accesstoken',
  'refreshtoken',
  'idtoken',
  'oauthtoken',
  'oauthcode',
  'authorization',
  'authorizationcode',
  'bearer',
  'token',
  'clientsecret',
  'password',
  'passwd',
  'apikey',
  'secret',
  'code',
])
const urlCredentialAssignment = /(?<![A-Za-z0-9])(?:(?:access|refresh|id|oauth)[_.-]?token|(?:oauth|authorization)[_.-]?code|authorization|bearer|token|client[_.-]?secret|password|passwd|api[_.-]?key|secret|code)(?![A-Za-z0-9])["']?\s*[:=]/i
const credentialValue = /(?:\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}|(?<![A-Za-z0-9])(?:(?:access|refresh|id|oauth)[_.-]?token|client[_.-]?(?:secret|assertion)|authorization|bearer|token|password|passwd|api[_.-]?key|secret|saml[_.-]?response)(?![A-Za-z0-9])["']?\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{12,}|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|-----BEGIN\s+[A-Z ]*PRIVATE\s+KEY-----)/i
const unsafeRawCanary = /(?:RAW|ATTACHMENT)_CANARY_DO_NOT_STORE/i
const htmlTag = /<\/?[A-Za-z][^>]*>/
const controlOrFormat = /[\p{Cc}\p{Cf}]/u
const emailAddress = /[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/
const mailHeaderPrefix = /^(?:from|to|cc|bcc|subject|sent|date):/im
const recipientAssignment = /(?<![A-Za-z0-9])["']?(?:to|cc|bcc|recipients?)["']?\s*[:=]/i
const urlRecipientNames = new Set(['to', 'cc', 'bcc', 'recipient', 'recipients'])
const validationPercentDecodeRounds = 5

function decodePercentOnce(value: string) {
  return value.replace(/(?:%[0-9a-f]{2})+/gi, (encoded) => {
    const bytes = encoded.split('%').slice(1).map((pair) => Number.parseInt(pair, 16))
    return new TextDecoder().decode(new Uint8Array(bytes))
  })
}

function decodeForValidation(value: string): string | null {
  let decoded = value
  for (let attempt = 0; attempt < validationPercentDecodeRounds; attempt += 1) {
    const candidate = decodePercentOnce(decoded)
    if (candidate === decoded) return decoded
    decoded = candidate
  }
  return decodePercentOnce(decoded) === decoded ? decoded : null
}

function decodedUrlContainsCredentialMaterial(decoded: string) {
  if (urlCredentialAssignment.test(decoded) || credentialValue.test(decoded)) return true
  try {
    const parsed = new URL(decoded)
    for (const component of [parsed.search.slice(1), parsed.hash.slice(1)]) {
      const params = new URLSearchParams(component)
      for (const key of params.keys()) {
        const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, '')
        if (urlCredentialNames.has(normalized)) return true
      }
    }
  } catch {
    return false
  }
  return false
}

function decodedUrlContainsRecipientMaterial(decoded: string) {
  if (recipientAssignment.test(decoded)) return true
  try {
    const parsed = new URL(decoded)
    for (const component of [parsed.search.slice(1), parsed.hash.slice(1)]) {
      const params = new URLSearchParams(component)
      for (const key of params.keys()) {
        const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, '')
        if (urlRecipientNames.has(normalized)) return true
      }
    }
  } catch {
    return false
  }
  return false
}

function isSafeMicrosoftUrl(value: string) {
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    return false
  }
  const decoded = decodeForValidation(value)
  if (decoded === null) return false
  const hostname = parsed.hostname.toLowerCase()
  const allowedHost = [
    '.microsoft.com',
    '.office.com',
    '.office365.com',
    '.microsoftonline.com',
    '.sharepoint.com',
    '.cloud.microsoft',
  ].some((suffix) => hostname.endsWith(suffix))
  return parsed.protocol === 'https:'
    && !parsed.username
    && !parsed.password
    && (!parsed.port || parsed.port === '443')
    && allowedHost
    && !controlOrFormat.test(decoded)
    && !htmlTag.test(decoded)
    && !unsafeRawCanary.test(decoded)
    && !emailAddress.test(decoded)
    && !decodedUrlContainsCredentialMaterial(decoded)
    && !decodedUrlContainsRecipientMaterial(decoded)
}

const safeMicrosoftUrl = z.string().max(4096).url().refine(
  isSafeMicrosoftUrl,
  'Receipt links must use a token-free, allowlisted Microsoft HTTPS host.',
)
const isoDateTime = z.string().datetime({ offset: true })
const boundedReference = z.string().min(1).max(512)
const safeSourceLocator = z.string().min(1).max(1024).refine((value) => {
  const decoded = decodeForValidation(value)
  return decoded !== null && !recipientAssignment.test(decoded)
}, 'Source locator must not contain recipient assignment material.')
const remoteMessageReferencePattern = /^[A-Za-z0-9][A-Za-z0-9._~:@/+%=-]{0,511}$/
const remoteMessageReference = z.string()
  .min(1)
  .max(512)
  .regex(remoteMessageReferencePattern, 'remote_message_ref must be an opaque Microsoft message identifier.')
  .refine((value) => !value.includes('://'), 'remote_message_ref must not be a URL.')
  .refine((value) => !emailAddress.test(value), 'remote_message_ref must not contain an email address.')
  .refine((value) => !/^(?:from|to|cc|bcc|subject|sent|date):/i.test(value), 'remote_message_ref must not contain a mail header.')
  .refine((value) => !/(?:RAW|ATTACHMENT)_CANARY_DO_NOT_STORE/i.test(value), 'remote_message_ref contains unsafe raw content.')
  .refine((value) => !/(?:access_token|refresh_token|id_token)[:=][A-Za-z0-9._~+/=-]{8,}/i.test(value), 'remote_message_ref must not contain credential material.')
  .refine((value) => !/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/.test(value), 'remote_message_ref must not contain a token.')
  .refine((value) => {
    const decoded = decodeForValidation(value)
    return decoded !== null
      && remoteMessageReferencePattern.test(decoded)
      && !controlOrFormat.test(decoded)
      && !htmlTag.test(decoded)
      && !unsafeRawCanary.test(decoded)
      && !urlCredentialAssignment.test(decoded)
      && !credentialValue.test(decoded)
      && !decoded.includes('://')
      && !emailAddress.test(decoded)
      && !/^(?:from|to|cc|bcc|subject|sent|date):/i.test(decoded)
      && !recipientAssignment.test(decoded)
  }, 'remote_message_ref contains unsafe encoded content.')

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

const sourceSchema = z
  .object({
    provider: z.string().min(1),
    resource_type: safeSourceLocator,
    connection_ref: safeSourceLocator,
    container_ref: safeSourceLocator,
    object_ref: safeSourceLocator,
    version_ref: safeSourceLocator,
    display_title: z.string().min(1),
    web_url: safeExternalUrl.nullable(),
    retrieved_at: z.string().min(1),
    fingerprint: sha256,
  })
  .strict()
  .superRefine((source, context) => {
    if (MICROSOFT_PROVIDERS.includes(source.provider as (typeof MICROSOFT_PROVIDERS)[number])
      && (source.web_url === null || !isSafeMicrosoftUrl(source.web_url))) {
      context.addIssue({
        code: 'custom',
        path: ['web_url'],
        message: 'Microsoft source links must use a token-free, allowlisted Microsoft HTTPS host.',
      })
    }
  })

const packetActionSchema = z
  .object({
    title: z.string().min(1),
    detail: z.string().default(''),
    priority: z.enum(TASK_PRIORITIES),
    due: z.string().nullable(),
  })
  .strict()

const captureActionSchema = z
  .object({
    id: z.string(),
    task_id: z.string().optional(),
    title: z.string().min(1),
    detail: z.string().default(''),
    priority: z.enum(TASK_PRIORITIES),
    due: z.string().nullable(),
  })
  .strict()

const normalizedSchema = z
  .object({
    summary: z.string().max(2000),
    context: z.string().max(4000),
    action_items: z.array(packetActionSchema).max(20),
    tags: z.array(z.string()),
  })
  .strict()

const manualProvenanceSchema = z
  .object({
    capture_mode: z.literal('manual'),
    adapter: z.string().min(1),
    adapter_version: z.string().min(1),
    redaction_policy_version: z.string().min(1),
    raw_retained: z.literal(false),
    created_at: z.string().min(1),
  })
  .strict()

const verifiedProvenanceSchema = z
  .object({
    capture_mode: z.literal('oob_verified'),
    adapter: z.string().min(1),
    adapter_version: z.string().min(1),
    model: z.string().min(1),
    prompt_version: z.string().min(1),
    redaction_policy_version: z.string().min(1),
    tool_trace_digest: sha256,
    allowed_tools: z.array(z.string().min(1)).min(1),
    raw_retained: z.literal(false),
    created_at: z.string().min(1),
  })
  .strict()

export const capturePacketSchema: z.ZodType<CapturePacket> = z
  .object({
    schema_version: z.literal('1.0'),
    source_key: sha256,
    source: sourceSchema,
    normalized: normalizedSchema,
    task_hints: z.array(z.string()),
    provenance: z.discriminatedUnion('capture_mode', [
      manualProvenanceSchema,
      verifiedProvenanceSchema,
    ]),
  })
  .strict()
  .superRefine((packet, context) => {
    if (packet.provenance.capture_mode !== 'oob_verified') return
    const requiredReadTool = packet.source.provider === 'microsoft-outlook'
      ? 'm365.outlook.read'
      : packet.source.provider === 'microsoft-teams'
        ? 'm365.teams.read'
        : null
    const tools = new Set(packet.provenance.allowed_tools)
    if (requiredReadTool === null
      || tools.size !== 2
      || !tools.has(requiredReadTool)
      || !tools.has('workstack.capture.write')) {
      context.addIssue({
        code: 'custom',
        path: ['provenance', 'allowed_tools'],
        message: 'OOB provenance must declare exactly the provider read tool and workstack.capture.write.',
      })
    }
  })

export const captureSchema = z
  .object({
    schema_version: z.literal('1.0'),
    source_key: sha256,
    source: sourceSchema,
    normalized: z
      .object({
        summary: z.string().max(2000),
        context: z.string().max(4000),
        action_items: z.array(captureActionSchema).max(20),
        tags: z.array(z.string()),
      })
      .strict(),
    task_hints: z.array(z.string()),
    provenance: z.discriminatedUnion('capture_mode', [
      manualProvenanceSchema,
      verifiedProvenanceSchema,
    ]),
    id: z.string(),
    status: z.enum(CAPTURE_STATUSES),
    linked_task_ids: z.array(z.string()).default([]),
    converted_task_ids: z.array(z.string()).default([]),
    revision: z.number().int().nonnegative().default(0),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict()

export const oobRequestSchema: z.ZodType<OobRequest> = z
  .object({
    request_id: z.string().uuid(),
    schema_version: z.literal('1.0'),
    provider: z.enum(MICROSOFT_PROVIDERS),
    operation: z.literal('search_and_capture'),
    query: z.string().trim().min(1).max(500).refine((value) => {
      const decoded = decodeForValidation(value)
      return decoded !== null
        && !controlOrFormat.test(decoded)
        && !htmlTag.test(decoded)
        && !unsafeRawCanary.test(decoded)
        && !urlCredentialAssignment.test(decoded)
        && !credentialValue.test(decoded)
        && !mailHeaderPrefix.test(decoded)
        && !recipientAssignment.test(decoded)
    }, 'Search query must not contain credentials, raw content, HTML, mail headers, or recipient lists.'),
    result_limit: z.number().int().min(1).max(10),
    requested_at: isoDateTime,
  })
  .strict()

export const replyTargetSchema = z
  .object({
    resource_type: boundedReference,
    connection_ref: boundedReference,
    container_ref: boundedReference,
    object_ref: boundedReference,
    version_ref: boundedReference,
  })
  .strict()

export const replyReceiptSchema: z.ZodType<ReplyReceipt> = z
  .object({
    schema_version: z.literal('1.0'),
    reply_id: boundedReference,
    provider: z.enum(MICROSOFT_PROVIDERS),
    outcome: z.enum(REPLY_OUTCOMES),
    remote_message_ref: remoteMessageReference.optional(),
    web_url: safeMicrosoftUrl.optional(),
    occurred_at: isoDateTime,
    body_digest: sha256,
    target_digest: sha256,
    error_code: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/).optional(),
  })
  .strict()

export const replyCommandSchema: z.ZodType<ReplyCommand> = z
  .object({
    id: boundedReference,
    task_id: boundedReference,
    capture_id: boundedReference,
    capture_revision: z.number().int().nonnegative(),
    provider: z.enum(MICROSOFT_PROVIDERS),
    capability: z.enum(['outlook.reply', 'teams.reply']),
    target: replyTargetSchema,
    body: z.string().min(1).max(12_000),
    body_digest: sha256,
    target_digest: sha256,
    state: z.enum(REPLY_STATES),
    approved_at: isoDateTime,
    receipt: replyReceiptSchema.nullable().default(null),
    created_at: isoDateTime,
    updated_at: isoDateTime,
  })
  .strict()
  .superRefine((command, context) => {
    const expectedCapability = command.provider === 'microsoft-outlook'
      ? 'outlook.reply'
      : 'teams.reply'
    if (command.capability !== expectedCapability) {
      context.addIssue({
        code: 'custom',
        path: ['capability'],
        message: `Expected ${expectedCapability} for ${command.provider}.`,
      })
    }
    if (command.receipt && (
      command.receipt.reply_id !== command.id
      || command.receipt.provider !== command.provider
      || command.receipt.body_digest !== command.body_digest
      || command.receipt.target_digest !== command.target_digest
    )) {
      context.addIssue({
        code: 'custom',
        path: ['receipt'],
        message: 'Receipt identity and digests must match the approved reply command.',
      })
    }
    if (command.state === 'approved' && command.receipt) {
      context.addIssue({ code: 'custom', path: ['receipt'], message: 'An approved command cannot have a terminal receipt.' })
    }
    if (command.state !== 'approved' && command.receipt?.outcome !== command.state) {
      context.addIssue({ code: 'custom', path: ['receipt'], message: 'A terminal command requires a matching receipt outcome.' })
    }
  })

export const taskDetailSchema = z.object({
  task: taskSchema,
  context: z.array(z.record(z.string(), z.unknown())),
  activity: z.array(z.record(z.string(), z.unknown())),
  replies: z.array(replyCommandSchema).default([]),
}).strict()

const forbiddenCaptureKeys = new Set([
  'body',
  'html',
  'content',
  'attachments',
  'raw',
  'transcript',
  'recipients',
])

export function findForbiddenCaptureKey(value: unknown, path = '$'): string | null {
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      const match = findForbiddenCaptureKey(value[index], `${path}[${index}]`)
      if (match) return match
    }
    return null
  }

  if (!value || typeof value !== 'object') return null
  for (const [key, child] of Object.entries(value)) {
    if (forbiddenCaptureKeys.has(key.toLowerCase())) return `${path}.${key}`
    const match = findForbiddenCaptureKey(child, `${path}.${key}`)
    if (match) return match
  }
  return null
}
