import { describe, expect, test } from 'vitest'

import {
  federationRuntimeStatusSchema,
  federatedEntityRefSchema,
  portfolioProjectionSchema,
} from './federationSchemas'

const profileId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const workspaceId = '11111111-1111-4111-8111-111111111111'
const observedAt = '2026-09-01T12:00:00+09:00'

function validProjection() {
  return {
    type: 'workstack-federation-portfolio',
    schema_version: '1.0',
    generated_at: observedAt,
    workspaces: [{
      profile_id: profileId,
      workspace_id: workspaceId,
      name: 'Engineering',
      state: 'ready',
      confirmed_generation: 8,
      observed_at: observedAt,
      stale: false,
    }],
    tasks: [{
      ref: { workspace_id: workspaceId, entity_type: 'task', entity_id: 'T-0001' },
      title: 'Stabilize federation',
      status: 'started',
      priority: 'P1',
      due: '2026-09-05',
      objective_refs: [],
      revision: 3,
      source_generation: 8,
      observed_at: observedAt,
      stale: false,
    }],
    unavailable_sources: [],
  } as const
}

describe('federation contract schemas', () => {
  test('accepts a structured entity reference and rejects flattened or extended identities', () => {
    expect(federatedEntityRefSchema.parse({
      workspace_id: workspaceId,
      entity_type: 'task',
      entity_id: 'T-0001',
    })).toEqual({ workspace_id: workspaceId, entity_type: 'task', entity_id: 'T-0001' })

    expect(federatedEntityRefSchema.safeParse(`${workspaceId}:task:T-0001`).success).toBe(false)
    expect(federatedEntityRefSchema.safeParse({
      workspace_id: workspaceId,
      entity_type: 'task',
      entity_id: 'T-0001',
      url: 'https://remote.invalid/task/T-0001',
    }).success).toBe(false)
    expect(federatedEntityRefSchema.safeParse({
      workspace_id: 'ABCDEF12-1111-4111-8111-111111111111',
      entity_type: 'task',
      entity_id: 'T-0001',
    }).success).toBe(false)
    expect(federatedEntityRefSchema.safeParse({
      workspace_id: '00000000-0000-0000-0000-000000000000',
      entity_type: 'task',
      entity_id: 'T-0001',
    }).success).toBe(false)
  })

  test('accepts bounded runtime diagnostics and excludes endpoint or credential material', () => {
    const status = {
      profile_id: profileId,
      state: 'ready',
      expected_workspace_id: workspaceId,
      actual_workspace_id: workspaceId,
      product_version: '1.0.6',
      protocol_version: 1,
      session_port: 24567,
      confirmed_generation: 8,
      observed_at: observedAt,
      error_code: null,
    }
    expect(federationRuntimeStatusSchema.parse(status)).toEqual(status)
    expect(federationRuntimeStatusSchema.safeParse({ ...status, ssh_command: 'ssh remote' }).success).toBe(false)
    expect(federationRuntimeStatusSchema.safeParse({ ...status, error_code: 'contains spaces' }).success).toBe(false)
  })

  test('validates a provenance-bearing read-only Portfolio projection', () => {
    const projection = validProjection()

    expect(portfolioProjectionSchema.parse(projection)).toEqual(projection)
    expect(portfolioProjectionSchema.safeParse({
      ...projection,
      tasks: [{ ...projection.tasks[0], ref: { ...projection.tasks[0].ref, entity_type: 'objective' } }],
    }).success).toBe(false)
    expect(portfolioProjectionSchema.safeParse({ ...projection, mutation_url: '/api/v1/tasks' }).success).toBe(false)
  })

  test('rejects duplicate, orphaned, cross-authority, and inconsistent projections', () => {
    const projection = validProjection()
    const otherProfileId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    const otherWorkspaceId = '22222222-2222-4222-8222-222222222222'
    const duplicateWorkspace = {
      ...projection.workspaces[0],
      profile_id: otherProfileId,
    }

    const invalidCases = [
      { ...projection, workspaces: [...projection.workspaces, duplicateWorkspace] },
      { ...projection, workspaces: [...projection.workspaces, { ...duplicateWorkspace, workspace_id: otherWorkspaceId, profile_id: profileId }] },
      { ...projection, tasks: [...projection.tasks, projection.tasks[0]] },
      { ...projection, tasks: [{ ...projection.tasks[0], ref: { ...projection.tasks[0].ref, workspace_id: otherWorkspaceId } }] },
      { ...projection, tasks: [{ ...projection.tasks[0], source_generation: 9 }] },
      { ...projection, tasks: [{ ...projection.tasks[0], stale: true }] },
      { ...projection, tasks: [{ ...projection.tasks[0], observed_at: '2026-09-01T12:00:01+09:00' }] },
      {
        ...projection,
        tasks: [{
          ...projection.tasks[0],
          objective_refs: [{ workspace_id: otherWorkspaceId, entity_type: 'objective', entity_id: 'O-1' }],
        }],
      },
      {
        ...projection,
        unavailable_sources: [{
          profile_id: profileId,
          expected_workspace_id: otherWorkspaceId,
          state: 'connecting',
          error_code: null,
        }],
      },
      { ...projection, workspaces: [{ ...projection.workspaces[0], state: 'ready', stale: true }] },
    ]

    for (const invalid of invalidCases) {
      expect(portfolioProjectionSchema.safeParse(invalid).success).toBe(false)
    }
  })
})
