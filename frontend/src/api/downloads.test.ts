import { expect, test, vi } from 'vitest'
import { downloadBackup, downloadTaskSnapshot } from './downloads'
import { jsonResponse } from '../test/fixtures'

const backupDigest = `sha256:${'a'.repeat(64)}`
const snapshotDigest = `sha256:${'b'.repeat(64)}`
const workspaceId = 'workspace-download-test'

function binaryResponse(headers: Record<string, string>) {
  return new Response(new Blob(['verified artifact']), { status: 200, headers })
}

test('accepts only a fully verified backup download envelope', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'download-csrf-token' } })
    if (url.endsWith('/api/v1/maintenance/backup')) {
      return binaryResponse({
        'Content-Type': 'application/zip',
        'Content-Disposition': 'attachment; filename="workstack-backup-20260831T000000Z-deadbeef.zip"',
        'X-WorkStack-Backup-Digest': backupDigest,
        'X-WorkStack-Workspace-Id': workspaceId,
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(downloadBackup(workspaceId)).resolves.toMatchObject({
    digest: backupDigest,
    filename: 'workstack-backup-20260831T000000Z-deadbeef.zip',
  })
})

test('refuses a backup whose verified workspace identity does not match', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'download-csrf-token' } })
    if (url.endsWith('/api/v1/maintenance/backup')) {
      return binaryResponse({
        'Content-Type': 'application/zip',
        'Content-Disposition': 'attachment; filename="workstack-backup-20260831T000000Z-deadbeef.zip"',
        'X-WorkStack-Backup-Digest': backupDigest,
        'X-WorkStack-Workspace-Id': 'another-workspace',
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(downloadBackup(workspaceId)).rejects.toMatchObject({
    code: 'backup_response_invalid',
  })
})

test('accepts a task snapshot only when digest and filename match the reviewed preview', async () => {
  const taskId = '00000000-0000-4000-8000-000000000001'
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'download-csrf-token' } })
    if (url.endsWith(`/api/v1/tasks/${taskId}/snapshot/export`)) {
      return binaryResponse({
        'Content-Disposition': `attachment; filename="${taskId}.workstack-task.json"`,
        'X-WorkStack-Snapshot-Digest': snapshotDigest,
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(downloadTaskSnapshot(taskId, 7, snapshotDigest)).resolves.toMatchObject({
    digest: snapshotDigest,
    filename: `${taskId}.workstack-task.json`,
  })
})

test('refuses a task snapshot whose response digest differs from the reviewed preview', async () => {
  const taskId = '00000000-0000-4000-8000-000000000002'
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/v1/session')) return jsonResponse({ data: { csrf_token: 'download-csrf-token' } })
    if (url.endsWith(`/api/v1/tasks/${taskId}/snapshot/export`)) {
      return binaryResponse({
        'Content-Disposition': `attachment; filename="${taskId}.workstack-task.json"`,
        'X-WorkStack-Snapshot-Digest': `sha256:${'c'.repeat(64)}`,
      })
    }
    throw new Error(`Unexpected request: ${url}`)
  }))

  await expect(downloadTaskSnapshot(taskId, 8, snapshotDigest)).rejects.toMatchObject({
    code: 'snapshot_response_invalid',
  })
})
