import { z } from 'zod'

import { canonicalFederationUuidSchema } from './federationSchemas'

export const CONNECTION_REGISTRY_SCHEMA_VERSION = 1 as const
export const MAX_CONNECTION_PROFILES = 128
export const MAX_DISCOVERED_SSH_ALIASES = 512

const invalidControlCharacters = /[\0\r\n]/
const unsafePathSegments = /(?:^|[\\/])\.\.?(?:[\\/]|$)/

const boundedRequiredString = (maximum: number) => z.string()
  .min(1)
  .max(maximum)
  .refine((value) => !invalidControlCharacters.test(value), {
    message: 'Control characters are not allowed',
  })

export const connectionProfileIdSchema = canonicalFederationUuidSchema
export const connectionWorkspaceIdSchema = canonicalFederationUuidSchema

export const connectionProfileLabelSchema = boundedRequiredString(100)
  .transform((value) => value.trim())
  .pipe(z.string().min(1).max(100))

export const sshHostAliasSchema = z.string()
  .min(1)
  .max(255)
  .regex(/^[A-Za-z0-9][A-Za-z0-9_.@-]{0,254}$/, {
    message: 'SSH host aliases may not contain options, whitespace, wildcards, or shell characters',
  })

export const remoteLinuxPathSchema = boundedRequiredString(4096)
  .refine((value) => value.startsWith('/'), {
    message: 'Remote paths must be absolute Linux paths',
  })
  .refine((value) => !unsafePathSegments.test(value), {
    message: "Remote paths may not contain '.' or '..' segments",
  })
  .refine((value) => value.replace(/\/+$/, '') !== '', {
    message: 'The Linux filesystem root is not an allowed workspace path',
  })

export const localDataPathSchema = boundedRequiredString(4096)
  .refine((value) => !/^(?:\\\\|\/\/|\\\\[?.]\\)/.test(value), {
    message: 'UNC and Windows device paths are not accepted',
  })
  .refine((value) => /^(?:[A-Za-z]:[\\/]|\/(?!\/))/.test(value), {
    message: 'Local data paths must be absolute',
  })
  .refine((value) => !/^(?:[A-Za-z]:[\\/]?|\/)$/i.test(value), {
    message: 'Filesystem and drive roots are not accepted as workspace paths',
  })
  .refine((value) => !unsafePathSegments.test(value), {
    message: "Local data paths may not contain '.' or '..' segments",
  })

const connectionProfileBase = {
  profile_id: connectionProfileIdSchema,
  label: connectionProfileLabelSchema,
  enabled: z.boolean(),
  live_updates: z.boolean(),
  expected_workspace_id: connectionWorkspaceIdSchema,
}

export const localConnectionProfileSchema = z.object({
  ...connectionProfileBase,
  kind: z.literal('local'),
  data_dir: localDataPathSchema,
}).strict().readonly()

export const sshConnectionProfileSchema = z.object({
  ...connectionProfileBase,
  kind: z.literal('ssh'),
  ssh_host_alias: sshHostAliasSchema,
  remote_app_dir: remoteLinuxPathSchema,
  remote_data_dir: remoteLinuxPathSchema,
  preferred_forward_port: z.number().int().min(1).max(65_535),
  remote_port: z.number().int().min(1).max(65_535),
}).strict().readonly()

export const connectionProfileSchema = z.discriminatedUnion('kind', [
  localConnectionProfileSchema,
  sshConnectionProfileSchema,
])

const connectionProfileDraftBase = {
  profile_id: connectionProfileIdSchema,
  label: connectionProfileLabelSchema,
  enabled: z.boolean(),
  live_updates: z.boolean(),
  expected_workspace_id: connectionWorkspaceIdSchema.nullable(),
}

export const localConnectionProfileDraftSchema = z.object({
  ...connectionProfileDraftBase,
  kind: z.literal('local'),
  data_dir: localDataPathSchema,
}).strict().readonly()

export const sshConnectionProfileDraftSchema = z.object({
  ...connectionProfileDraftBase,
  kind: z.literal('ssh'),
  ssh_host_alias: sshHostAliasSchema,
  remote_app_dir: remoteLinuxPathSchema,
  remote_data_dir: remoteLinuxPathSchema,
  preferred_forward_port: z.number().int().min(1).max(65_535),
  remote_port: z.number().int().min(1).max(65_535),
}).strict().readonly()

/** A candidate may omit its authority only until the read-only Test operation detects it. */
export const connectionProfileDraftSchema = z.discriminatedUnion('kind', [
  localConnectionProfileDraftSchema,
  sshConnectionProfileDraftSchema,
])

export const connectionRegistrySchema = z.object({
  schema_version: z.literal(CONNECTION_REGISTRY_SCHEMA_VERSION),
  active_profile_id: connectionProfileIdSchema.nullable(),
  profiles: z.array(connectionProfileSchema).max(MAX_CONNECTION_PROFILES).readonly(),
}).strict().superRefine((registry, context) => {
  const profileIds = new Map<string, { enabled: boolean; index: number }>()
  const enabledAuthorities = new Set<string>()

  registry.profiles.forEach((profile, index) => {
    if (profileIds.has(profile.profile_id)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Connection profile IDs must be unique',
        path: ['profiles', index, 'profile_id'],
      })
    } else {
      profileIds.set(profile.profile_id, { enabled: profile.enabled, index })
    }

    if (profile.enabled && enabledAuthorities.has(profile.expected_workspace_id)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Only one enabled profile may own an expected workspace authority',
        path: ['profiles', index, 'expected_workspace_id'],
      })
    }
    if (profile.enabled) enabledAuthorities.add(profile.expected_workspace_id)
  })

  if (registry.profiles.length > 0 && registry.active_profile_id === null) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'A non-empty registry must identify one active profile',
      path: ['active_profile_id'],
    })
  }
  if (registry.active_profile_id !== null) {
    const active = profileIds.get(registry.active_profile_id)
    if (active === undefined) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'The active profile must exist in the registry',
        path: ['active_profile_id'],
      })
    } else if (!active.enabled) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'The active profile must be enabled',
        path: ['active_profile_id'],
      })
    }
  }
}).readonly()

export type LocalConnectionProfile = z.infer<typeof localConnectionProfileSchema>
export type SshConnectionProfile = z.infer<typeof sshConnectionProfileSchema>
export type ConnectionProfile = z.infer<typeof connectionProfileSchema>
export type ConnectionProfileDraft = z.infer<typeof connectionProfileDraftSchema>
export type ConnectionRegistry = z.infer<typeof connectionRegistrySchema>
