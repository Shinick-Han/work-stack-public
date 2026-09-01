import { z } from 'zod'

import { TASK_PRIORITIES, TASK_STATUSES } from './types'

export const FEDERATION_SCHEMA_VERSION = '1.0' as const
export const MAX_FEDERATED_WORKSPACES = 128
export const MAX_PORTFOLIO_TASKS = 128
export const MAX_PORTFOLIO_OBJECTIVE_REFS = 16

export const canonicalFederationUuidSchema = z.string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
  .refine((value) => value !== '00000000-0000-0000-0000-000000000000', {
    message: 'UUID must be canonical and non-nil',
  })
const boundedIdentifier = z.string().min(1).max(128).regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/)
const observedAt = z.string().datetime({ offset: true })

export const federatedEntityTypeSchema = z.enum(['task', 'objective', 'note'])

export const federatedEntityRefSchema = z.object({
  workspace_id: canonicalFederationUuidSchema,
  entity_type: federatedEntityTypeSchema,
  entity_id: boundedIdentifier,
}).strict().readonly()

export const federationConnectionStateSchema = z.enum([
  'idle',
  'connecting',
  'ready',
  'stale',
  'reconnecting',
  'disconnected',
  'identity-mismatch',
  'integrity-blocked',
  'incompatible',
])

const boundedErrorCode = z.string()
  .min(1)
  .max(64)
  .regex(/^[a-z0-9][a-z0-9_.-]*$/)

export const federationRuntimeStatusSchema = z.object({
  profile_id: canonicalFederationUuidSchema,
  state: federationConnectionStateSchema,
  expected_workspace_id: canonicalFederationUuidSchema,
  actual_workspace_id: canonicalFederationUuidSchema.nullable(),
  product_version: z.string().max(64).nullable(),
  protocol_version: z.number().int().nonnegative().nullable(),
  session_port: z.number().int().min(1).max(65_535).nullable(),
  confirmed_generation: z.number().int().nonnegative().nullable(),
  observed_at: observedAt.nullable(),
  error_code: boundedErrorCode.nullable(),
}).strict().readonly()

export const federationStatusMessageSchema = z.object({
  type: z.literal('workstack-federation-status'),
  schema_version: z.literal(FEDERATION_SCHEMA_VERSION),
  active_profile_id: canonicalFederationUuidSchema.nullable(),
  statuses: z.array(federationRuntimeStatusSchema).max(MAX_FEDERATED_WORKSPACES).readonly(),
}).strict().superRefine((message, context) => {
  const profileIds = new Set<string>()
  for (const [index, status] of message.statuses.entries()) {
    if (profileIds.has(status.profile_id)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Runtime status profile_id values must be unique',
        path: ['statuses', index, 'profile_id'],
      })
    }
    profileIds.add(status.profile_id)
  }
  if (message.active_profile_id !== null && !profileIds.has(message.active_profile_id)) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'active_profile_id must identify one reported profile',
      path: ['active_profile_id'],
    })
  }
}).readonly()

export const portfolioWorkspaceSchema = z.object({
  profile_id: canonicalFederationUuidSchema,
  workspace_id: canonicalFederationUuidSchema,
  name: z.string().min(1).max(200),
  state: federationConnectionStateSchema,
  confirmed_generation: z.number().int().nonnegative(),
  observed_at: observedAt,
  stale: z.boolean(),
}).strict().superRefine((workspace, context) => {
  if (workspace.state === 'ready' && workspace.stale) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'A ready workspace cannot be stale',
      path: ['stale'],
    })
  }
  if (workspace.state !== 'ready' && !workspace.stale) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'A non-ready workspace projection must be stale',
      path: ['stale'],
    })
  }
}).readonly()

export const portfolioTaskSchema = z.object({
  ref: federatedEntityRefSchema.refine((ref) => ref.entity_type === 'task', {
    message: 'Portfolio task references must have entity_type task',
  }),
  title: z.string().min(1).max(500),
  status: z.enum(TASK_STATUSES),
  priority: z.enum(TASK_PRIORITIES),
  due: z.string().date().nullable(),
  objective_refs: z.array(
    federatedEntityRefSchema.refine((ref) => ref.entity_type === 'objective', {
      message: 'Objective references must have entity_type objective',
    }),
  ).max(MAX_PORTFOLIO_OBJECTIVE_REFS).readonly(),
  revision: z.number().int().nonnegative(),
  source_generation: z.number().int().nonnegative(),
  observed_at: observedAt,
  stale: z.boolean(),
}).strict().readonly()

export const portfolioUnavailableSourceSchema = z.object({
  profile_id: canonicalFederationUuidSchema,
  expected_workspace_id: canonicalFederationUuidSchema,
  state: federationConnectionStateSchema.exclude(['ready']),
  error_code: boundedErrorCode.nullable(),
}).strict().readonly()

function entityRefKey(ref: z.infer<typeof federatedEntityRefSchema>): string {
  return `${ref.workspace_id}/${ref.entity_type}/${ref.entity_id}`
}

/**
 * A disposable, read-only aggregate. It deliberately contains no mutation
 * commands, connection details, raw URLs, filesystem paths, or SSH arguments.
 */
export const portfolioProjectionSchema = z.object({
  type: z.literal('workstack-federation-portfolio'),
  schema_version: z.literal(FEDERATION_SCHEMA_VERSION),
  generated_at: observedAt,
  workspaces: z.array(portfolioWorkspaceSchema).max(MAX_FEDERATED_WORKSPACES).readonly(),
  tasks: z.array(portfolioTaskSchema).max(MAX_PORTFOLIO_TASKS).readonly(),
  unavailable_sources: z.array(portfolioUnavailableSourceSchema).max(MAX_FEDERATED_WORKSPACES).readonly(),
}).strict().superRefine((projection, context) => {
  const workspacesById = new Map<string, z.infer<typeof portfolioWorkspaceSchema>>()
  const availableProfileIds = new Set<string>()
  for (const [index, workspace] of projection.workspaces.entries()) {
    if (availableProfileIds.has(workspace.profile_id)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Portfolio workspace profile_id values must be unique',
        path: ['workspaces', index, 'profile_id'],
      })
    }
    if (workspacesById.has(workspace.workspace_id)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Portfolio workspace_id values must be unique',
        path: ['workspaces', index, 'workspace_id'],
      })
    }
    availableProfileIds.add(workspace.profile_id)
    workspacesById.set(workspace.workspace_id, workspace)
  }

  const unavailableProfileIds = new Set<string>()
  const unavailableWorkspaceIds = new Set<string>()
  for (const [index, source] of projection.unavailable_sources.entries()) {
    if (availableProfileIds.has(source.profile_id) || unavailableProfileIds.has(source.profile_id)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'A profile may appear only once across available and unavailable sources',
        path: ['unavailable_sources', index, 'profile_id'],
      })
    }
    if (workspacesById.has(source.expected_workspace_id) || unavailableWorkspaceIds.has(source.expected_workspace_id)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'A workspace authority may appear only once in a Portfolio projection',
        path: ['unavailable_sources', index, 'expected_workspace_id'],
      })
    }
    unavailableProfileIds.add(source.profile_id)
    unavailableWorkspaceIds.add(source.expected_workspace_id)
  }

  const taskKeys = new Set<string>()
  for (const [index, task] of projection.tasks.entries()) {
    const key = entityRefKey(task.ref)
    if (taskKeys.has(key)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Portfolio task references must be unique',
        path: ['tasks', index, 'ref'],
      })
    }
    taskKeys.add(key)

    const workspace = workspacesById.get(task.ref.workspace_id)
    if (workspace === undefined) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Portfolio tasks must belong to a projected workspace',
        path: ['tasks', index, 'ref', 'workspace_id'],
      })
      continue
    }
    if (task.source_generation !== workspace.confirmed_generation) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Task generation must match its workspace projection',
        path: ['tasks', index, 'source_generation'],
      })
    }
    if (task.observed_at !== workspace.observed_at) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Task observation time must match its workspace projection',
        path: ['tasks', index, 'observed_at'],
      })
    }
    if (task.stale !== workspace.stale) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Task stale state must match its workspace projection',
        path: ['tasks', index, 'stale'],
      })
    }

    const objectiveKeys = new Set<string>()
    for (const [objectiveIndex, objectiveRef] of task.objective_refs.entries()) {
      if (objectiveRef.workspace_id !== task.ref.workspace_id) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Cross-workspace objective references are not available in schema version 1.0',
          path: ['tasks', index, 'objective_refs', objectiveIndex, 'workspace_id'],
        })
      }
      const objectiveKey = entityRefKey(objectiveRef)
      if (objectiveKeys.has(objectiveKey)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Objective references within a Task must be unique',
          path: ['tasks', index, 'objective_refs', objectiveIndex],
        })
      }
      objectiveKeys.add(objectiveKey)
    }
  }
}).readonly()

export type FederatedEntityType = z.infer<typeof federatedEntityTypeSchema>
export type FederatedEntityRef = z.infer<typeof federatedEntityRefSchema>
export type FederationConnectionState = z.infer<typeof federationConnectionStateSchema>
export type FederationRuntimeStatus = z.infer<typeof federationRuntimeStatusSchema>
export type FederationStatusMessage = z.infer<typeof federationStatusMessageSchema>
export type PortfolioWorkspace = z.infer<typeof portfolioWorkspaceSchema>
export type PortfolioTask = z.infer<typeof portfolioTaskSchema>
export type PortfolioProjection = z.infer<typeof portfolioProjectionSchema>
