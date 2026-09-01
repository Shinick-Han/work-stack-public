import type { BackupDownload, SnapshotDownload } from '../domain/types'
import { ApiError, assertOk, fetchWithNetworkRetry, getCsrfToken } from './transport'

async function postDownload(path: string, body: unknown, accept: string): Promise<Response> {
  const encodedBody = JSON.stringify(body)
  const makeHeaders = async (refreshSession = false) => ({
    Accept: accept,
    'Content-Type': 'application/json',
    'X-WorkStack-CSRF': await getCsrfToken(refreshSession),
  })
  let response = await fetchWithNetworkRetry(path, {
    method: 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: await makeHeaders(),
    body: encodedBody,
  })
  if (response.status === 403) {
    response = await fetchWithNetworkRetry(path, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: await makeHeaders(true),
      body: encodedBody,
    })
  }
  return response
}

export async function downloadBackup(expectedWorkspaceId: string): Promise<BackupDownload> {
  const response = await postDownload(
    '/api/v1/maintenance/backup',
    { confirmed: true },
    'application/zip',
  )
  if (!response.ok) {
    await assertOk(response)
    throw new ApiError(response.status, 'backup_download_failed', 'Backup download failed.')
  }
  const digest = response.headers.get('X-WorkStack-Backup-Digest')
  const workspaceId = response.headers.get('X-WorkStack-Workspace-Id')
  const contentType = response.headers.get('Content-Type')
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const filename = /^attachment; filename="(workstack-backup-[0-9TZ]+-[0-9a-f]{8}\.zip)"$/.exec(disposition)?.[1]
  if (contentType !== 'application/zip' || !digest?.match(/^sha256:[0-9a-f]{64}$/) || workspaceId !== expectedWorkspaceId || !filename) {
    throw new ApiError(
      response.status,
      'backup_response_invalid',
      'The backup response could not be verified.',
    )
  }
  return { blob: await response.blob(), digest, filename }
}

export async function downloadTaskSnapshot(
  taskId: string,
  expectedRevision: number,
  expectedDigest: string,
): Promise<SnapshotDownload> {
  const path = `/api/v1/tasks/${encodeURIComponent(taskId)}/snapshot/export`
  const response = await postDownload(
    path,
    {
      disclosure_confirmed: true,
      expected_digest: expectedDigest,
      expected_revision: expectedRevision,
    },
    'application/json',
  )
  if (!response.ok) {
    await assertOk(response)
    throw new ApiError(response.status, 'snapshot_export_failed', 'Snapshot export failed.')
  }
  const digest = response.headers.get('X-WorkStack-Snapshot-Digest')
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const filename = /^attachment; filename="([0-9a-f-]{36}\.workstack-task\.json)"$/.exec(disposition)?.[1]
  if (digest !== expectedDigest || !filename) {
    throw new ApiError(
      response.status,
      'snapshot_response_invalid',
      'The snapshot download response could not be verified.',
    )
  }
  return { blob: await response.blob(), digest, filename }
}
