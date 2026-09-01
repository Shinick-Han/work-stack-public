import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef } from 'react'
import { api, createIdempotencyKey } from '../api/client'
import type { SyncStatus, WorkspaceRebindResult } from '../domain/types'
import { coordinateSsotWorkspaceRebind, notifySsotWorkspaceRebound } from './ssotHostBridge'

interface RebindOperation {
  coordinate: string
  key: string
}

interface WorkspaceRebindOptions {
  onRecovered: (result: WorkspaceRebindResult) => void
  open: boolean
  status?: SyncStatus
}

function statusCoordinate(status?: SyncStatus) {
  return status ? `${status.generation}:${status.manifest_digest ?? ''}` : ''
}

function operationFor(current: RebindOperation, coordinate: string): RebindOperation {
  return current.coordinate === coordinate
    ? current
    : { coordinate, key: createIdempotencyKey() }
}

export function useWorkspaceRebind({ onRecovered, open, status }: WorkspaceRebindOptions) {
  const queryClient = useQueryClient()
  const coordinate = statusCoordinate(status)
  const operationRef = useRef<RebindOperation>({ coordinate: '', key: createIdempotencyKey() })
  const previewQuery = useQuery({
    queryKey: ['sync-rebind-preview', coordinate],
    queryFn: api.getWorkspaceRebindPreview,
    enabled: open && Boolean(status?.rebind_available),
    retry: false,
    refetchOnWindowFocus: false,
  })
  const mutation = useMutation({
    mutationFn: async ({ idempotencyKey }: { idempotencyKey: string }) => {
      if (!previewQuery.data) throw new Error('Workspace recovery preview is unavailable')
      await coordinateSsotWorkspaceRebind(previewQuery.data.candidate_workspace_id)
      return api.rebindWorkspace(previewQuery.data, idempotencyKey)
    },
    onSuccess: async (result) => {
      notifySsotWorkspaceRebound(result.workspace_id)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['sync-status'] }),
        queryClient.invalidateQueries({ queryKey: ['sync-rebind-preview'] }),
        queryClient.invalidateQueries({ queryKey: ['workspace'] }),
        queryClient.invalidateQueries({ queryKey: ['captures'] }),
        queryClient.invalidateQueries({ queryKey: ['task'] }),
        queryClient.invalidateQueries({ queryKey: ['review'] }),
      ])
      onRecovered(result)
    },
  })

  const begin = () => {
    operationRef.current = { coordinate, key: createIdempotencyKey() }
    mutation.reset()
  }
  const refresh = () => {
    operationRef.current = { coordinate: '', key: createIdempotencyKey() }
    mutation.reset()
    void previewQuery.refetch()
  }
  const run = () => {
    if (!previewQuery.data) return
    const reviewedCoordinate = `${coordinate}:${previewQuery.data.candidate_digest}`
    operationRef.current = operationFor(operationRef.current, reviewedCoordinate)
    mutation.mutate({ idempotencyKey: operationRef.current.key })
  }

  return { begin, mutation, previewQuery, refresh, run }
}
