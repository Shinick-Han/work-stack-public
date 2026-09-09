import { z } from 'zod'
import {
  KNOWLEDGE_IMPORT_ADAPTER,
  KNOWLEDGE_IMPORT_PROVIDER,
  KNOWLEDGE_IMPORT_RESOURCE_TYPE,
  captureRetrievalProjectionSchema,
} from './schemaKnowledgeCapture'
import {
  CAPTURE_STATUSES,
  MICROSOFT_PROVIDERS,
  REPLY_OUTCOMES,
  REPLY_STATES,
  TASK_PRIORITIES,
  type Capture,
  type CapturePacket,
  type OobRequest,
  type ReplyCommand,
  type ReplyReceipt,
} from './types'
import {
  boundedReference,
  controlOrFormat,
  credentialValue,
  decodeForValidation,
  emailAddress,
  htmlTag,
  isoDateTime,
  isSafeMicrosoftUrl,
  mailHeaderPrefix,
  recipientAssignment,
  safeExternalUrl,
  safeMicrosoftUrl,
  safeSourceLocator,
  sha256,
  unsafeRawCanary,
  urlCredentialAssignment,
} from './schemaPrimitives'

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

const storedNormalizedSchema = z
  .object({
    summary: z.string().max(2000),
    context: z.string().max(4000),
    action_items: z.array(captureActionSchema).max(20),
    tags: z.array(z.string()),
  })
  .strict()

const captureRecordFields = {
  source_key: sha256,
  source: sourceSchema,
  normalized: storedNormalizedSchema,
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
}

const knowledgeImportListedSourceSchema = z.object({
  provider: z.literal(KNOWLEDGE_IMPORT_PROVIDER),
  resource_type: z.literal(KNOWLEDGE_IMPORT_RESOURCE_TYPE),
  connection_ref: safeSourceLocator,
  container_ref: safeSourceLocator,
  object_ref: safeSourceLocator,
  version_ref: safeSourceLocator,
  display_title: z.string().min(1),
  web_url: z.null(),
  retrieved_at: z.string().min(1),
  fingerprint: sha256,
}).strict()

const knowledgeImportProvenanceSchema = z.object({
  capture_mode: z.literal('manual'),
  adapter: z.literal(KNOWLEDGE_IMPORT_ADAPTER),
  adapter_version: z.string().min(1),
  redaction_policy_version: z.string().min(1),
  raw_retained: z.literal(false),
  created_at: z.string().min(1),
}).strict()

const captureV10Schema = z.object({
  schema_version: z.literal('1.0'),
  ...captureRecordFields,
}).strict()

const captureV11Schema = z.object({
  schema_version: z.literal('1.1'),
  source_key: sha256,
  source: knowledgeImportListedSourceSchema,
  normalized: storedNormalizedSchema,
  task_hints: z.array(z.string()),
  provenance: knowledgeImportProvenanceSchema,
  id: z.string(),
  status: z.enum(CAPTURE_STATUSES),
  linked_task_ids: z.array(z.string()).default([]),
  converted_task_ids: z.array(z.string()).default([]),
  revision: z.number().int().nonnegative().default(0),
  created_at: z.string(),
  updated_at: z.string(),
  retrieval: captureRetrievalProjectionSchema,
}).strict()

export const captureSchema: z.ZodType<Capture> = z.discriminatedUnion('schema_version', [
  captureV10Schema,
  captureV11Schema,
])

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
