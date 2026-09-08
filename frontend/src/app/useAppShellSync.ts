import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { useMutation, useQuery, type QueryClient } from '@tanstack/react-query'
import { api, ApiError, createIdempotencyKey } from '../api/client'
import type { SyncStatus } from '../domain/types'
import { getErrorMessage } from '../utils/format'
import { isSyncUnavailable, isSyncWriteBlocked } from './SyncStatusControl'
import { useWorkspaceRebind } from './useWorkspaceRebind'
import type { MicrosoftOobMode } from '../features/integrations/MicrosoftOobDialog'

interface SyncAdoptOperation {
  coordinate: string
  key: string
}

export function operationForCoordinate(current: SyncAdoptOperation, coordinate: string): SyncAdoptOperation {
  return current.coordinate === coordinate ? current : { coordinate, key: createIdempotencyKey() }
}

export function syncCoordinateFor(status?: SyncStatus): string {
  return status ? `${status.generation}:${status.manifest_digest ?? ''}` : ''
}

export function firstErrorMessage(primary: unknown, secondary: unknown): string | null {
  if (primary) return getErrorMessage(primary)
  if (secondary) return getErrorMessage(secondary)
  return null
}

export function deriveSyncPresentation(syncError: unknown, syncStatus: SyncStatus | undefined, workspaceFailed: boolean) {
  const legacyEndpointUnavailable = syncError instanceof ApiError
    && syncError.status === 404
    && !syncStatus
    && !workspaceFailed
  const effectiveError = syncError && !legacyEndpointUnavailable ? syncError : null
  return {
    effectiveError,
    endpointDisconnected: Boolean(effectiveError && !isSyncUnavailable(effectiveError)),
    writeBlocked: isSyncWriteBlocked(syncStatus, effectiveError),
  }
}

export function refreshAuthoritativeQueries(queryClient: QueryClient) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ['sync-status'] }),
    queryClient.invalidateQueries({ queryKey: ['workspace'] }),
    queryClient.invalidateQueries({ queryKey: ['captures'] }),
    queryClient.invalidateQueries({ queryKey: ['task'] }),
    queryClient.invalidateQueries({ queryKey: ['review'] }),
  ])
}

export function useAutoOpenWorkspaceRebind({
  coordinate,
  onOpen,
  rebindAvailable,
  workspaceError,
  workspaceLoaded,
}: {
  coordinate: string
  onOpen: () => void
  rebindAvailable?: boolean
  workspaceError: boolean
  workspaceLoaded: boolean
}) {
  const openedCoordinateRef = useRef('')
  useEffect(() => {
    if (!workspaceError || workspaceLoaded || !rebindAvailable || !coordinate) return
    if (openedCoordinateRef.current === coordinate) return
    openedCoordinateRef.current = coordinate
    onOpen()
  }, [coordinate, onOpen, rebindAvailable, workspaceError, workspaceLoaded])
}

function useSyncAdoptMutation(
  queryClient: QueryClient,
  showNotice: (message: string) => void,
  setSyncReviewOpen: Dispatch<SetStateAction<boolean>>,
) {
  return useMutation({
    mutationFn: ({ generation, idempotencyKey, manifestDigest }: { generation: number; idempotencyKey: string; manifestDigest: string }) => (
      api.adoptSyncChanges(generation, manifestDigest, idempotencyKey)
    ),
    onSuccess: async () => {
      await refreshAuthoritativeQueries(queryClient)
      setSyncReviewOpen(false)
      showNotice('Validated SSOT changes accepted')
    },
  })
}

function beginSyncReview(
  syncAdoptOperationRef: { current: SyncAdoptOperation },
  syncAdoptMutation: { reset: () => void },
  workspaceRebind: { begin: () => void },
  setSyncReviewOpen: Dispatch<SetStateAction<boolean>>,
  syncCoordinate: string,
) {
  syncAdoptOperationRef.current = { coordinate: syncCoordinate, key: createIdempotencyKey() }
  syncAdoptMutation.reset()
  workspaceRebind.begin()
  setSyncReviewOpen(true)
}

function refreshSyncReview(
  syncAdoptOperationRef: { current: SyncAdoptOperation },
  syncAdoptMutation: { reset: () => void },
  workspaceRebind: { refresh: () => void },
  queryClient: QueryClient,
) {
  syncAdoptOperationRef.current = { coordinate: '', key: createIdempotencyKey() }
  syncAdoptMutation.reset()
  workspaceRebind.refresh()
  refreshAuthoritativeQueries(queryClient)
}

function adoptReviewedSync(
  syncAdoptOperationRef: { current: SyncAdoptOperation },
  syncAdoptMutation: { mutate: (input: { generation: number; idempotencyKey: string; manifestDigest: string }) => void },
  syncStatus: SyncStatus | undefined,
  syncCoordinate: string,
) {
  if (!syncStatus?.manifest_digest) return
  syncAdoptOperationRef.current = operationForCoordinate(syncAdoptOperationRef.current, syncCoordinate)
  syncAdoptMutation.mutate({
    generation: syncStatus.generation,
    idempotencyKey: syncAdoptOperationRef.current.key,
    manifestDigest: syncStatus.manifest_digest,
  })
}

export function useAppShellWriteBlock({
  selectedCaptureId,
  setImportOpen,
  setMicrosoftOobMode,
  setQuickTaskOpen,
  setWorkspaceActionsOpen,
  syncWriteBlocked,
  taskId,
}: {
  selectedCaptureId?: string
  setImportOpen: Dispatch<SetStateAction<boolean>>
  setMicrosoftOobMode: Dispatch<SetStateAction<MicrosoftOobMode | null>>
  setQuickTaskOpen: Dispatch<SetStateAction<boolean>>
  setWorkspaceActionsOpen: Dispatch<SetStateAction<boolean>>
  syncWriteBlocked: boolean
  taskId: string | null
}) {
  useEffect(() => {
    const drawers = Array.from(document.querySelectorAll<HTMLElement>('.detail-drawer'))
    for (const drawer of drawers) drawer.inert = syncWriteBlocked
    if (syncWriteBlocked) {
      setQuickTaskOpen(false)
      setWorkspaceActionsOpen(false)
      setImportOpen(false)
      setMicrosoftOobMode(null)
    }
    return () => {
      for (const drawer of drawers) drawer.inert = false
    }
  }, [selectedCaptureId, taskId, syncWriteBlocked])
}

export function useAppShellSync({
  queryClient,
  showNotice,
  workspaceFailed,
}: {
  queryClient: QueryClient
  showNotice: (message: string) => void
  workspaceFailed: boolean
}) {
  const syncAdoptOperationRef = useRef({ coordinate: '', key: createIdempotencyKey() })
  const [syncReviewOpen, setSyncReviewOpen] = useState(false)
  const syncStatusQuery = useQuery({
    queryKey: ['sync-status'],
    queryFn: api.getSyncStatus,
    retry: false,
    refetchOnWindowFocus: false,
  })
  const syncAdoptMutation = useSyncAdoptMutation(queryClient, showNotice, setSyncReviewOpen)
  const workspaceRebind = useWorkspaceRebind({
    open: syncReviewOpen,
    status: syncStatusQuery.data,
    onRecovered: (result) => {
      setSyncReviewOpen(false)
      showNotice(`Workspace reconnected · receipt ${result.recovery_receipt_digest.slice(7, 19)}`)
    },
  })
  const syncStatus = syncStatusQuery.data
  const syncCoordinate = syncCoordinateFor(syncStatus)
  const presentation = deriveSyncPresentation(syncStatusQuery.error, syncStatus, workspaceFailed)
  return {
    ...presentation,
    adoptReviewedSync: () => adoptReviewedSync(syncAdoptOperationRef, syncAdoptMutation, syncStatus, syncCoordinate),
    beginSyncReview: () => beginSyncReview(syncAdoptOperationRef, syncAdoptMutation, workspaceRebind, setSyncReviewOpen, syncCoordinate),
    refreshSyncReview: () => refreshSyncReview(syncAdoptOperationRef, syncAdoptMutation, workspaceRebind, queryClient),
    setSyncReviewOpen,
    syncAdoptMutation,
    syncCoordinate,
    syncReviewOpen,
    syncStatus,
    syncStatusQuery,
    workspaceRebind,
    workspaceRebindError: firstErrorMessage(workspaceRebind.mutation.error, workspaceRebind.previewQuery.error),
  }
}
