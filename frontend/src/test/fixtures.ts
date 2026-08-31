import type { Capture, Task, WorkspaceProjection } from '../domain/types'

export const task: Task = {
  id: 'T-0001',
  uid: '11111111-1111-1111-8111-111111111111',
  title: 'Define release quality gate',
  detail: 'Make release criteria measurable.',
  status: 'started',
  priority: 'P0',
  due: '2026-09-01',
  scheduled: '2026-08-31',
  estimate_minutes: 90,
  tags: ['quality'],
  objective_ids: ['O-1'],
  parent_id: null,
  dependencies: [],
  subtasks: [],
  notes: [],
  revision: 2,
  context_count: 1,
}

export const workspace: WorkspaceProjection = {
  schema_version: '1.0',
  workspace: { id: '22222222-2222-2222-2222-222222222222', name: 'Work Stack' },
  tasks: [task],
  objectives: [{ id: 'O-1', objective: 'Release quality customers trust', status: 'active', revision: 0 }],
  notes: [],
  edges: [],
  inbox_count: 1,
}

export const capture: Capture = {
  id: 'C-0001',
  schema_version: '1.0',
  source_key: `sha256:${'a'.repeat(64)}`,
  source: {
    provider: 'microsoft-outlook',
    resource_type: 'mail.message',
    connection_ref: 'personal-outlook',
    container_ref: 'mailbox:demo',
    object_ref: 'message:demo',
    version_ref: 'change-key:v1',
    display_title: 'Release review feedback',
    web_url: 'https://outlook.office.com/mail/deeplink/read/demo',
    retrieved_at: '2026-08-29T08:00:00Z',
    fingerprint: `sha256:${'b'.repeat(64)}`,
  },
  normalized: {
    summary: 'Rollback verification needs an owner.',
    context: 'This continues the release-quality discussion.',
    action_items: [{ id: 'A-0001', title: 'Add rollback check', detail: 'Update the checklist.', priority: 'P1', due: null }],
    tags: ['release'],
  },
  task_hints: ['T-0001'],
  provenance: {
    capture_mode: 'manual',
    adapter: 'manual-import',
    adapter_version: '1.0.0',
    redaction_policy_version: 'workstack-redaction-v1',
    raw_retained: false,
    created_at: '2026-08-29T08:00:03Z',
  },
  status: 'inbox',
  linked_task_ids: [],
  converted_task_ids: [],
  revision: 0,
  created_at: '2026-08-29T08:00:03Z',
  updated_at: '2026-08-29T08:00:03Z',
}

export function jsonResponse(data: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }))
}
