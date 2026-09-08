import { useCallback, type Dispatch, type SetStateAction } from 'react'

import { requestKnowledge } from './knowledgeHostBridge'
import { isAmbiguousKnowledgePin, isKnowledgeCancelled, isKnowledgeSourceConflict } from './knowledgeErrors'
import {
  applyCatalog,
  applySearchMatch,
  applySearchResult,
  conflictPinState,
  currentKnowledgeBinding,
  emptyKnowledgeForm,
  failState,
  normalizeKnowledgeSearchQuery,
  parseKnowledgeLineSpan,
  previewMatchesInput,
  reconcileAfterAmbiguousPin,
  type KnowledgePanelState,
} from './knowledgeSession'
import {
  KNOWLEDGE_SEARCH_TIMEOUT_MS,
  type KnowledgeReadReference,
  type KnowledgeSavedReference,
  type KnowledgeTaskRef,
} from './knowledgeTypes'
import type { useKnowledgeFlight } from './useKnowledgeFlight'

const STATUS_TIMEOUT_MS = 15_000
const PICKER_TIMEOUT_MS = 300_000

type Flight = ReturnType<typeof useKnowledgeFlight>
type SetKnowledgeState = Dispatch<SetStateAction<KnowledgePanelState>>

export function useKnowledgeVaultActions(
  flight: Flight,
  setState: SetKnowledgeState,
  load: (signal: AbortSignal) => Promise<{ references: KnowledgePanelState['references']; vaults: KnowledgePanelState['vaults'] }>,
) {
  const { begin, forgetInFlight, run } = flight
  const connectVault = useCallback(() => {
    const { signal, token } = begin('choose-vault')
    void run(token, () => requestKnowledge('choose-vault', {}, PICKER_TIMEOUT_MS, signal), (data) => {
      if (data.cancelled) {
        setState((current) => ({
          ...current,
          notice: 'Vault picker closed without choosing a folder.',
          pending: null,
        }))
        return
      }
      setState((current) => ({
        ...current,
        ...emptyKnowledgeForm(),
        pending: null,
        selectedVaultId: data.vault.vault_id,
        vaults: current.vaults.some((vault) => vault.vault_id === data.vault.vault_id)
          ? current.vaults
          : [...current.vaults, data.vault],
      }))
    })
  }, [begin, run, setState])

  const selectVault = useCallback((vaultId: string) => {
    forgetInFlight()
    setState((current) => ({
      ...current,
      ...emptyKnowledgeForm(),
      error: null,
      errorAction: null,
      errorCode: null,
      notice: null,
      pending: null,
      selectedVaultId: vaultId,
    }))
  }, [forgetInFlight, setState])

  const reload = useCallback(() => {
    const { signal, token } = begin('load')
    void run(token, () => load(signal), ({ references, vaults }) => {
      setState((current) => applyCatalog(current, vaults, references))
    })
  }, [begin, load, run, setState])

  return { connectVault, reload, selectVault }
}

export function useKnowledgeReferenceActions(
  flight: Flight,
  setState: SetKnowledgeState,
  workspaceUid: string,
  task: KnowledgeTaskRef,
  state: KnowledgePanelState,
) {
  const { begin, run } = flight
  const preview = useCallback((documentPath: string, startLine: string, endLine: string) => {
    const vaultId = state.selectedVaultId
    let span: { end_line: number; start_line: number }
    try {
      span = parseKnowledgeLineSpan(startLine, endLine)
    } catch (error) {
      setState((current) => failState(current, error))
      return
    }
    const { signal, token } = begin('preview')
    const binding = currentKnowledgeBinding(workspaceUid, task)
    void run(token, () => requestKnowledge('read-reference', {
      binding,
      document_path: documentPath.trim(),
      end_line: span.end_line,
      start_line: span.start_line,
      vault_id: vaultId,
    }, STATUS_TIMEOUT_MS, signal), (data) => {
      setState((current) => ({ ...current, opened: null, pending: null, preview: data.reference }))
    })
  }, [begin, run, setState, state.selectedVaultId, task, workspaceUid])

  const link = useCallback((reason: string) => {
    const previewValue = state.preview
    if (!previewMatchesInput(previewValue, state.selectedVaultId, state.documentPath, state.startLine, state.endLine)) {
      setState((current) => ({
        ...current,
        error: 'Preview the current path and span before linking.',
        errorAction: null,
        errorCode: 'preview_required',
      }))
      return
    }
    const { signal, token } = begin('pin')
    const binding = currentKnowledgeBinding(workspaceUid, task)
    void run(token, async () => {
      try {
        return {
          status: 'pinned' as const,
          data: await requestKnowledge('pin-reference', {
            binding,
            document_path: previewValue.document_path,
            end_line: previewValue.end_line,
            expected_sha256: previewValue.source_sha256,
            reason: reason.trim(),
            start_line: previewValue.start_line,
            vault_id: previewValue.vault_id,
          }, STATUS_TIMEOUT_MS, signal),
        }
      } catch (error) {
        if (isKnowledgeCancelled(error)) throw error
        if (isKnowledgeSourceConflict(error)) return { status: 'conflict' as const, error }
        if (!isAmbiguousKnowledgePin(error)) throw error
        try {
          const listed = await requestKnowledge('list-references', { binding }, STATUS_TIMEOUT_MS, signal)
          return { status: 'reconciled' as const, error, listed }
        } catch (listedError) {
          if (isKnowledgeCancelled(listedError)) throw listedError
          throw error
        }
      }
    }, (result) => {
      if (result.status === 'pinned') {
        setState((current) => ({
          ...current,
          pending: null,
          reason: '',
          references: [
            ...current.references.filter((item) => item.reference_id !== result.data.reference.reference_id),
            result.data.reference,
          ],
        }))
        return
      }
      if (result.status === 'conflict') {
        setState((current) => conflictPinState(current, result.error))
        return
      }
      setState((current) => reconcileAfterAmbiguousPin(current, result.listed.references, result.error))
    })
  }, [begin, run, setState, state.documentPath, state.endLine, state.preview, state.selectedVaultId, state.startLine, task, workspaceUid])

  return { link, preview }
}

export function useKnowledgeSavedActions(
  flight: Flight,
  setState: SetKnowledgeState,
  workspaceUid: string,
  task: KnowledgeTaskRef,
) {
  const { begin, run } = flight
  const readReference = useCallback((reference: KnowledgeSavedReference) => {
    const { signal, token } = begin('read')
    const binding = currentKnowledgeBinding(workspaceUid, task)
    void run(token, () => requestKnowledge('read-reference', {
      binding,
      document_path: reference.document_path,
      end_line: reference.end_line,
      expected_sha256: reference.source_sha256,
      start_line: reference.start_line,
      vault_id: reference.vault_id,
    }, STATUS_TIMEOUT_MS, signal), (data) => {
      setState((current) => ({
        ...current,
        opened: { read: data.reference, referenceId: reference.reference_id },
        pending: null,
      }))
    })
  }, [begin, run, setState, task, workspaceUid])

  const unlink = useCallback((referenceId: string) => {
    const { signal, token } = begin('unpin')
    const binding = currentKnowledgeBinding(workspaceUid, task)
    void run(token, () => requestKnowledge('unpin-reference', {
      binding,
      reference_id: referenceId,
    }, STATUS_TIMEOUT_MS, signal), () => {
      setState((current) => ({
        ...current,
        opened: current.opened?.referenceId === referenceId ? null : current.opened,
        pending: null,
        references: current.references.filter((item) => item.reference_id !== referenceId),
      }))
    })
  }, [begin, run, setState, task, workspaceUid])

  return { readReference, unlink }
}

export function useKnowledgeSearchActions(
  flight: Flight,
  setState: SetKnowledgeState,
  workspaceUid: string,
  task: KnowledgeTaskRef,
  state: KnowledgePanelState,
) {
  const { begin, run } = flight
  const setSearchQuery = useCallback((searchQuery: string) => {
    setState((current) => ({ ...current, error: null, searchQuery }))
  }, [setState])

  const search = useCallback(() => {
    const vaultId = state.selectedVaultId
    if (!vaultId) {
      setState((current) => ({
        ...current,
        error: 'Select a vault before searching.',
        errorAction: null,
        errorCode: 'vault_required',
      }))
      return
    }
    let query: string
    try {
      query = normalizeKnowledgeSearchQuery(state.searchQuery)
    } catch (error) {
      setState((current) => failState({ ...current, pending: 'search' }, error))
      return
    }
    const { signal, token } = begin('search')
    const binding = currentKnowledgeBinding(workspaceUid, task)
    void run(token, () => requestKnowledge('search-references', {
      binding,
      query,
      vault_id: vaultId,
    }, KNOWLEDGE_SEARCH_TIMEOUT_MS, signal), (data) => {
      setState((current) => applySearchResult(current, data, query, vaultId))
    })
  }, [begin, run, setState, state.searchQuery, state.selectedVaultId, task, workspaceUid])

  const selectMatch = useCallback((match: KnowledgeReadReference) => {
    setState((current) => applySearchMatch(current, match))
  }, [setState])

  return { search, selectMatch, setSearchQuery }
}
