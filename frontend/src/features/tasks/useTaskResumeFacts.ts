/**
 * Reads the recorded checkpoint a Task resumes from.
 *
 * The one thing this hook adds to `selectTaskResumeFacts` is the read itself,
 * and it deliberately adds nothing else. It reuses the validated workspace
 * audit under the key Daily Review and the checkpoint notices already use, so
 * the Task drawer and the handoff panel share one cache entry and one in-flight
 * request instead of racing two views of the same records.
 *
 * It never writes, never invalidates and never retries by itself: recording
 * progress and checkpoint transitions already invalidate this key where those
 * writes live, and the only retry offered here is one the reader asks for.
 *
 * Facts are derived from the arguments of the current render, so a Task or
 * workspace change re-derives against the new binding in the same pass. There
 * is no moment where the previous Task's next step is on screen under the new
 * Task's name.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import {
  selectTaskResumeFacts,
  taskResumeAuditQueryKey,
  type TaskResumeFacts,
} from './taskResumeFacts'

export interface TaskResumeFactsResult extends TaskResumeFacts {
  /** Re-reads the workspace audit. Explicit only; nothing here auto-retries. */
  retry: () => void
}

export function useTaskResumeFacts(workspaceUid: string, taskId: string): TaskResumeFactsResult {
  // Without both coordinates there is nothing to bind a record to, so no read
  // is started and the snapshot stays an honest empty one.
  const enabled = workspaceUid !== '' && taskId !== ''
  const query = useQuery({
    queryKey: taskResumeAuditQueryKey(workspaceUid),
    queryFn: () => api.getCheckpointAudit(),
    enabled,
  })
  const facts = selectTaskResumeFacts(workspaceUid, taskId, {
    // A disabled query still reports itself as pending. Reading its state only
    // while it is enabled keeps "no Task selected" from looking like loading.
    audit: enabled ? query.data : undefined,
    isPending: enabled && query.isPending,
    error: enabled ? query.error : null,
  })
  return { ...facts, retry: () => { void query.refetch() } }
}
