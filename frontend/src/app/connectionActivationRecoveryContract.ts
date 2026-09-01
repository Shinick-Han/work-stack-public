import { z } from 'zod'

import { canonicalFederationUuidSchema } from '../domain/federationSchemas'
import { connectionRegistryDigestSchema } from './connectionRegistryHostBridge'

export const CONNECTION_ACTIVATION_RECOVERY_SCHEMA_VERSION = 1 as const

const sensitiveAssignment = /\b(?:password|passwd|token|secret|authorization|cookie)\s*[:=]/i
const absolutePath = /(?:[A-Za-z]:[\\/]|\\\\|\/(?:Users|home|srv|var|etc|opt|mnt|tmp)(?:\/|$))/i
const commandSyntax = /(?:`|\$\(|&&|\|\||;\s*(?:cmd|powershell|bash|sh|ssh)\b)/i

export const recoveryDisplayTextSchema = z.string()
  .trim()
  .min(1)
  .max(240)
  .refine((value) => !/[\0-\x1f\x7f]/.test(value), 'Control characters are not allowed')
  .refine((value) => !absolutePath.test(value), 'Filesystem paths are not allowed')
  .refine((value) => !sensitiveAssignment.test(value), 'Credential material is not allowed')
  .refine((value) => !commandSyntax.test(value), 'Command text is not allowed')

const recoveryProfileSchema = z.object({
  profile_id: canonicalFederationUuidSchema,
  label: recoveryDisplayTextSchema.pipe(z.string().max(100)),
  kind: z.enum(['local', 'ssh']),
}).strict().readonly()

export const connectionActivationRecoveryStateSchema = z.object({
  type: z.literal('workstack-connection-activation-recovery-state'),
  schema_version: z.literal(CONNECTION_ACTIVATION_RECOVERY_SCHEMA_VERSION),
  activation_id: canonicalFederationUuidSchema,
  expected_registry_digest: connectionRegistryDigestSchema,
  failed_profile: recoveryProfileSchema,
  previous_profile: recoveryProfileSchema.nullable(),
  error: z.object({
    code: z.string().min(1).max(64).regex(/^[a-z0-9_.-]+$/),
    summary: recoveryDisplayTextSchema,
  }).strict().readonly(),
}).strict().readonly()

const actionEnvelope = {
  type: z.literal('workstack-connection-activation-recovery-request'),
  schema_version: z.literal(CONNECTION_ACTIVATION_RECOVERY_SCHEMA_VERSION),
  request_id: canonicalFederationUuidSchema,
  activation_id: canonicalFederationUuidSchema,
}

export const connectionActivationRecoveryRequestSchema = z.discriminatedUnion('operation', [
  z.object({
    ...actionEnvelope,
    operation: z.literal('restore-previous-connection'),
    expected_registry_digest: connectionRegistryDigestSchema,
  }).strict(),
  z.object({
    ...actionEnvelope,
    operation: z.literal('exit'),
  }).strict(),
])

export type ConnectionActivationRecoveryState = z.infer<typeof connectionActivationRecoveryStateSchema>
export type ConnectionActivationRecoveryRequest = z.infer<typeof connectionActivationRecoveryRequestSchema>
export type ConnectionActivationRecoveryOperation = ConnectionActivationRecoveryRequest['operation']

export function createConnectionActivationRecoveryRequest(
  state: ConnectionActivationRecoveryState,
  operation: ConnectionActivationRecoveryOperation,
  requestId: string = window.crypto.randomUUID(),
): ConnectionActivationRecoveryRequest {
  const envelope = {
    type: 'workstack-connection-activation-recovery-request' as const,
    schema_version: CONNECTION_ACTIVATION_RECOVERY_SCHEMA_VERSION,
    request_id: requestId,
    activation_id: state.activation_id,
  }
  return connectionActivationRecoveryRequestSchema.parse(operation === 'restore-previous-connection'
    ? { ...envelope, operation, expected_registry_digest: state.expected_registry_digest }
    : { ...envelope, operation })
}
