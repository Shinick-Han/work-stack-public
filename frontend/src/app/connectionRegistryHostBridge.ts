import { z } from 'zod'

import {
  CONNECTION_REGISTRY_SCHEMA_VERSION,
  MAX_DISCOVERED_SSH_ALIASES,
  connectionProfileDraftSchema,
  connectionProfileIdSchema,
  connectionRegistrySchema,
  localDataPathSchema,
  sshHostAliasSchema,
  type ConnectionRegistry,
} from '../domain/connectionRegistrySchemas'

export const MAX_CONNECTION_REGISTRY_REQUEST_BYTES = 1024 * 1024
export const MAX_CONNECTION_REGISTRY_RESPONSE_BYTES = 2 * 1024 * 1024
export const connectionRegistryDigestSchema = z.string().regex(/^sha256:[0-9a-f]{64}$/)
const connectionTestProofSchema = connectionProfileIdSchema
export type ConnectionRegistryDigest = z.infer<typeof connectionRegistryDigestSchema>

interface WebViewMessageEvent extends Event { data?: unknown }
interface ConnectionRegistryHostWindow extends Window {
  chrome?: {
    webview?: {
      addEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      removeEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      postMessage: (message: string) => void
    }
  }
}

const requestEnvelope = {
  type: z.literal('workstack-connection-registry-request'),
  schema_version: z.literal(CONNECTION_REGISTRY_SCHEMA_VERSION),
  request_id: connectionProfileIdSchema,
}

export const connectionRegistryHostRequestSchema = z.discriminatedUnion('operation', [
  z.object({
    ...requestEnvelope,
    operation: z.literal('get-registry'),
  }).strict(),
  z.object({
    ...requestEnvelope,
    operation: z.literal('save-registry'),
    registry: connectionRegistrySchema,
    expected_registry_digest: connectionRegistryDigestSchema,
  }).strict(),
  z.object({
    ...requestEnvelope,
    operation: z.literal('discover-ssh-aliases'),
  }).strict(),
  z.object({
    ...requestEnvelope,
    operation: z.literal('choose-local-directory'),
  }).strict(),
  z.object({
    ...requestEnvelope,
    operation: z.literal('test-profile'),
    profile: connectionProfileDraftSchema,
    base_registry_digest: connectionRegistryDigestSchema,
  }).strict(),
  z.object({
    ...requestEnvelope,
    operation: z.literal('activate-profile'),
    registry: connectionRegistrySchema,
    profile_id: connectionProfileIdSchema,
    proof_id: connectionTestProofSchema,
    expected_registry_digest: connectionRegistryDigestSchema,
  }).strict().superRefine((request, context) => {
    if (!request.registry.profiles.some((profile) => profile.profile_id === request.profile_id)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'The activated profile must exist in the candidate registry',
        path: ['profile_id'],
      })
    }
    if (request.registry.active_profile_id !== request.profile_id) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'The candidate registry must select the activated profile',
        path: ['registry', 'active_profile_id'],
      })
    }
  }),
])

const responseEnvelope = {
  type: z.literal('workstack-connection-registry-response'),
  schema_version: z.literal(CONNECTION_REGISTRY_SCHEMA_VERSION),
  request_id: connectionProfileIdSchema,
}

const getRegistryResultSchema = z.object({
  ...responseEnvelope,
  operation: z.literal('get-registry'),
  ok: z.literal(true),
  result: z.object({
    registry: connectionRegistrySchema.nullable(),
    registry_digest: connectionRegistryDigestSchema,
  }).strict().readonly(),
}).strict().readonly()

const saveRegistryResultSchema = z.object({
  ...responseEnvelope,
  operation: z.literal('save-registry'),
  ok: z.literal(true),
  result: z.object({
    registry: connectionRegistrySchema,
    registry_digest: connectionRegistryDigestSchema,
  }).strict().readonly(),
}).strict().readonly()

const sshAliasDiscoveryResultSchema = z.object({
  ...responseEnvelope,
  operation: z.literal('discover-ssh-aliases'),
  ok: z.literal(true),
  result: z.object({
    aliases: z.array(sshHostAliasSchema).max(MAX_DISCOVERED_SSH_ALIASES).readonly(),
  }).strict().readonly(),
}).strict().superRefine((result, context) => {
  const aliases = new Set<string>()
  result.result.aliases.forEach((alias, index) => {
    const key = alias.toLocaleLowerCase('en-US')
    if (aliases.has(key)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Discovered aliases must be unique (case-insensitive)',
        path: ['result', 'aliases', index],
      })
    }
    aliases.add(key)
  })
}).readonly()

const chooseLocalDirectoryResultSchema = z.object({
  ...responseEnvelope,
  operation: z.literal('choose-local-directory'),
  ok: z.literal(true),
  result: z.object({ selection: localDataPathSchema.nullable() }).strict().readonly(),
}).strict().readonly()

const testProfileResultSchema = z.object({
  ...responseEnvelope,
  operation: z.literal('test-profile'),
  ok: z.literal(true),
  result: z.object({
    profile_id: connectionProfileIdSchema,
    kind: z.enum(['local', 'ssh']),
    status: z.enum(['ready', 'candidate', 'identity_mismatch']),
    actual_workspace_id: connectionProfileIdSchema.nullable(),
    product_version: z.string().min(1).max(64).nullable(),
    protocol_version: z.number().int().min(0).nullable(),
    proof_id: connectionTestProofSchema.nullable(),
  }).strict().superRefine((result, context) => {
    const candidateHasAuthority = result.actual_workspace_id !== null
    if ((result.status === 'candidate') === candidateHasAuthority) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Candidate status and detected workspace identity are inconsistent',
        path: ['status'],
      })
    }
    if ((result.product_version === null) !== (result.protocol_version === null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Product and protocol versions must both be present or both be absent',
        path: ['product_version'],
      })
    }
    if ((result.proof_id !== null) !== (result.status === 'ready')) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Only a ready test must issue an activation proof',
        path: ['proof_id'],
      })
    }
  }).readonly(),
}).strict().readonly()

const activateProfileResultSchema = z.object({
  ...responseEnvelope,
  operation: z.literal('activate-profile'),
  ok: z.literal(true),
  result: z.object({
    registry: connectionRegistrySchema,
    registry_digest: connectionRegistryDigestSchema,
    restart_required: z.literal(true),
  }).strict().readonly(),
}).strict().readonly()

const registryErrorSchema = z.object({
  type: z.literal('workstack-connection-registry-response'),
  schema_version: z.literal(CONNECTION_REGISTRY_SCHEMA_VERSION),
  request_id: connectionProfileIdSchema.nullable(),
  operation: z.enum([
    'get-registry',
    'save-registry',
    'discover-ssh-aliases',
    'choose-local-directory',
    'test-profile',
    'activate-profile',
  ]).nullable(),
  ok: z.literal(false),
  error: z.object({
    code: z.string().min(1).max(64).refine((value) => !/[\0-\x1f]/.test(value)),
    message: z.string().min(1).max(256).refine((value) => !/[\0-\x1f]/.test(value)),
  }).strict().readonly(),
}).strict().superRefine((response, context) => {
  if ((response.request_id === null) !== (response.operation === null)) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'Error correlation fields must both be present or both be null',
      path: ['request_id'],
    })
  }
}).readonly()

export const connectionRegistryHostMessageSchema = z.union([
  getRegistryResultSchema,
  saveRegistryResultSchema,
  sshAliasDiscoveryResultSchema,
  chooseLocalDirectoryResultSchema,
  testProfileResultSchema,
  activateProfileResultSchema,
  registryErrorSchema,
])

export type ConnectionRegistryHostRequest = z.infer<typeof connectionRegistryHostRequestSchema>
export type ConnectionRegistryHostMessage = z.infer<typeof connectionRegistryHostMessageSchema>

const hostWindow = () => window as ConnectionRegistryHostWindow

export function hasConnectionRegistryHost(): boolean {
  const bridge = hostWindow().chrome?.webview
  return typeof bridge?.postMessage === 'function' && typeof bridge.addEventListener === 'function'
}

function serializedByteLength(value: unknown): number | null {
  try {
    const serialized = JSON.stringify(value)
    return serialized === undefined ? null : new TextEncoder().encode(serialized).byteLength
  } catch {
    return null
  }
}

function createRequestId(): string {
  return window.crypto.randomUUID()
}

function send(request: ConnectionRegistryHostRequest): string | null {
  const bridge = hostWindow().chrome?.webview
  if (typeof bridge?.postMessage !== 'function') return null
  const validated = connectionRegistryHostRequestSchema.parse(request)
  const serialized = JSON.stringify(validated)
  if (new TextEncoder().encode(serialized).byteLength > MAX_CONNECTION_REGISTRY_REQUEST_BYTES) {
    throw new RangeError('Connection registry request exceeds the native bridge limit')
  }
  bridge.postMessage(serialized)
  return validated.request_id
}

export function requestConnectionRegistry(requestId: string = createRequestId()): string | null {
  return send({
    type: 'workstack-connection-registry-request',
    schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
    request_id: requestId,
    operation: 'get-registry',
  })
}

export function saveConnectionRegistry(
  registry: ConnectionRegistry,
  expectedRegistryDigest: ConnectionRegistryDigest,
  requestId: string = createRequestId(),
): string | null {
  return send({
    type: 'workstack-connection-registry-request',
    schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
    request_id: requestId,
    operation: 'save-registry',
    registry,
    expected_registry_digest: expectedRegistryDigest,
  })
}

export function requestSshAliasDiscovery(requestId: string = createRequestId()): string | null {
  return send({
    type: 'workstack-connection-registry-request',
    schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
    request_id: requestId,
    operation: 'discover-ssh-aliases',
  })
}

export function requestLocalDirectoryChoice(requestId: string = createRequestId()): string | null {
  return send({
    type: 'workstack-connection-registry-request',
    schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
    request_id: requestId,
    operation: 'choose-local-directory',
  })
}

export function requestConnectionProfileTest(
  profile: z.infer<typeof connectionProfileDraftSchema>,
  baseRegistryDigest: ConnectionRegistryDigest,
  requestId: string = createRequestId(),
): string | null {
  return send({
    type: 'workstack-connection-registry-request',
    schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
    request_id: requestId,
    operation: 'test-profile',
    profile,
    base_registry_digest: baseRegistryDigest,
  })
}

export function activateConnectionProfile(
  registry: ConnectionRegistry,
  profileId: string,
  proofId: string,
  expectedRegistryDigest: ConnectionRegistryDigest,
  requestId: string = createRequestId(),
): string | null {
  return send({
    type: 'workstack-connection-registry-request',
    schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
    request_id: requestId,
    operation: 'activate-profile',
    registry,
    profile_id: profileId,
    proof_id: proofId,
    expected_registry_digest: expectedRegistryDigest,
  })
}

export function subscribeConnectionRegistryHostMessages(
  listener: (message: ConnectionRegistryHostMessage) => void,
): () => void {
  const bridge = hostWindow().chrome?.webview
  if (!bridge?.addEventListener) return () => undefined
  const receive = (event: WebViewMessageEvent) => {
    const byteLength = serializedByteLength(event.data)
    if (byteLength === null || byteLength > MAX_CONNECTION_REGISTRY_RESPONSE_BYTES) return
    const parsed = connectionRegistryHostMessageSchema.safeParse(event.data)
    if (parsed.success) listener(parsed.data)
  }
  bridge.addEventListener('message', receive)
  return () => bridge.removeEventListener?.('message', receive)
}
