import { describe, expect, test } from 'vitest'

import {
  CONNECTION_REGISTRY_SCHEMA_VERSION,
  MAX_CONNECTION_PROFILES,
  connectionProfileDraftSchema,
  connectionRegistrySchema,
} from './connectionRegistrySchemas'

const profileId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const workspaceId = '11111111-1111-4111-8111-111111111111'

const localProfile = {
  profile_id: profileId,
  label: 'Local workspace',
  kind: 'local',
  enabled: true,
  live_updates: true,
  expected_workspace_id: workspaceId,
  data_dir: 'C:\\WorkStack\\ssot',
} as const

function registry(profiles: readonly unknown[] = [localProfile], activeProfileId: string | null = profileId) {
  return { schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION, active_profile_id: activeProfileId, profiles }
}

describe('connection registry schemas', () => {
  test('accepts the version 1 profile fields and rejects extensions', () => {
    expect(connectionRegistrySchema.parse(registry())).toEqual(registry())
    expect(connectionRegistrySchema.safeParse({ ...registry(), raw_ssh_command: 'ssh remote' }).success).toBe(false)
    expect(connectionRegistrySchema.safeParse(registry([{ ...localProfile, token: 'secret' }])).success).toBe(false)
    expect(connectionRegistrySchema.safeParse({ ...registry(), schema_version: '1.0' }).success).toBe(false)
  })

  test('requires canonical non-nil UUIDs', () => {
    expect(connectionRegistrySchema.safeParse(registry([{ ...localProfile, profile_id: profileId.toUpperCase() }], profileId.toUpperCase())).success).toBe(false)
    expect(connectionRegistrySchema.safeParse(registry([{ ...localProfile, expected_workspace_id: '00000000-0000-0000-0000-000000000000' }])).success).toBe(false)
  })

  test('allows only a test candidate to await detected workspace identity', () => {
    const candidate = { ...localProfile, expected_workspace_id: null }
    expect(connectionProfileDraftSchema.safeParse(candidate).success).toBe(true)
    expect(connectionRegistrySchema.safeParse(registry([candidate])).success).toBe(false)
    expect(connectionProfileDraftSchema.safeParse({ ...candidate, raw_command: 'ssh host' }).success).toBe(false)
  })

  test('rejects unsafe aliases and paths', () => {
    const sshProfile = {
      profile_id: localProfile.profile_id,
      label: localProfile.label,
      kind: 'ssh',
      enabled: localProfile.enabled,
      live_updates: localProfile.live_updates,
      expected_workspace_id: localProfile.expected_workspace_id,
      ssh_host_alias: 'work-linux',
      remote_app_dir: '/srv/workstack/app',
      remote_data_dir: '/srv/workstack/ssot',
      preferred_forward_port: 24_567,
      remote_port: 8_765,
    }
    expect(connectionRegistrySchema.safeParse(registry([sshProfile])).success).toBe(true)
    for (const invalid of [
      { ...sshProfile, ssh_host_alias: '-oProxyCommand=calc' },
      { ...sshProfile, ssh_host_alias: 'work *' },
      { ...sshProfile, remote_data_dir: '/' },
      { ...sshProfile, remote_app_dir: '/srv/../etc' },
    ]) expect(connectionRegistrySchema.safeParse(registry([invalid])).success).toBe(false)
    expect(connectionRegistrySchema.safeParse(registry([{ ...localProfile, data_dir: '\\\\server\\share' }])).success).toBe(false)
    expect(connectionRegistrySchema.safeParse(registry([{ ...localProfile, data_dir: 'C:\\' }])).success).toBe(false)
    expect(connectionRegistrySchema.safeParse(registry([{ ...localProfile, data_dir: '/' }])).success).toBe(false)
  })

  test('enforces the profile count bound', () => {
    const profiles = Array.from({ length: MAX_CONNECTION_PROFILES + 1 }, (_, index) => ({
      ...localProfile,
      profile_id: `aaaaaaaa-aaaa-4aaa-8aaa-${(index + 1).toString(16).padStart(12, '0')}`,
      expected_workspace_id: `11111111-1111-4111-8111-${(index + 1).toString(16).padStart(12, '0')}`,
    }))
    expect(connectionRegistrySchema.safeParse(registry(profiles)).success).toBe(false)
  })

  test('rejects duplicate enabled workspace authorities', () => {
    const second = { ...localProfile, profile_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' }
    expect(connectionRegistrySchema.safeParse(registry([localProfile, second])).success).toBe(false)
    expect(connectionRegistrySchema.safeParse(registry([localProfile, { ...second, enabled: false }])).success).toBe(true)
  })

  test('rejects a disabled active profile', () => {
    expect(connectionRegistrySchema.safeParse(registry([{ ...localProfile, enabled: false }])).success).toBe(false)
    expect(connectionRegistrySchema.safeParse(registry([], null)).success).toBe(true)
    expect(connectionRegistrySchema.safeParse(registry([localProfile], null)).success).toBe(false)
  })
})
