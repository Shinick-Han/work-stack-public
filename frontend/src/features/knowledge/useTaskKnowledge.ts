import { useCallback, useEffect, useRef, useState } from 'react'

import { knowledgeHostAvailable, requestKnowledge } from './knowledgeHostBridge'
import {
  applyCatalog,
  currentKnowledgeBinding,
  emptyKnowledgeForm,
  initialKnowledgeState,
} from './knowledgeSession'
import type { KnowledgeTaskRef } from './knowledgeTypes'
import {
  useKnowledgeReferenceActions,
  useKnowledgeSavedActions,
  useKnowledgeSearchActions,
  useKnowledgeVaultActions,
} from './useKnowledgeActions'
import { useKnowledgeFlight } from './useKnowledgeFlight'

const STATUS_TIMEOUT_MS = 15_000

function loadCatalog(workspaceUid: string, task: KnowledgeTaskRef, signal: AbortSignal) {
  const binding = currentKnowledgeBinding(workspaceUid, task)
  return Promise.all([
    requestKnowledge('status', {}, STATUS_TIMEOUT_MS, signal),
    requestKnowledge('list-references', { binding }, STATUS_TIMEOUT_MS, signal),
  ]).then(([status, listed]) => ({ references: listed.references, vaults: status.vaults }))
}

export function useTaskKnowledge(workspaceUid: string, task: KnowledgeTaskRef) {
  const [state, setState] = useState(initialKnowledgeState)
  const flight = useKnowledgeFlight(setState)
  const taskKey = `${workspaceUid}:${task.uid}:${task.id}`
  const bindingKey = `${taskKey}:${task.revision}`
  const previousTaskKey = useRef('')
  const previousBindingKey = useRef('')

  useEffect(() => {
    const switchedTask = previousTaskKey.current !== taskKey
    const switchedBinding = previousBindingKey.current !== bindingKey
    previousTaskKey.current = taskKey
    previousBindingKey.current = bindingKey
    const binding = currentKnowledgeBinding(workspaceUid, task)
    const { signal, token } = flight.begin('load')
    const available = knowledgeHostAvailable()
    setState((current) => ({
      ...(switchedTask
        ? { ...current, ...emptyKnowledgeForm(), references: [], searchQuery: task.title }
        : {
          ...current,
          // A revision-only change is still a new Task snapshot: evidence read under the
          // previous revision must not survive to authorize a pin on the new one.
          opened: switchedBinding ? null : current.opened,
          preview: switchedBinding ? null : current.preview,
          search: null,
        }),
      binding,
      error: null,
      errorAction: null,
      errorCode: null,
      hostAvailable: available,
      loading: available,
      notice: null,
      pending: available ? 'load' : null,
    }))
    if (!available) return () => { flight.abortRef.current?.abort() }
    void flight.run(token, () => loadCatalog(workspaceUid, task, signal), ({ references, vaults }) => {
      setState((current) => applyCatalog(current, vaults, references))
    })
    return () => { flight.abortRef.current?.abort() }
  }, [bindingKey, task.id, task.revision, task.uid, taskKey, workspaceUid])

  const vault = useKnowledgeVaultActions(
    flight,
    setState,
    (signal) => loadCatalog(workspaceUid, task, signal),
  )
  const references = useKnowledgeReferenceActions(flight, setState, workspaceUid, task, state)
  const saved = useKnowledgeSavedActions(flight, setState, workspaceUid, task)
  const search = useKnowledgeSearchActions(flight, setState, workspaceUid, task, state)

  const setDocumentPath = useCallback((documentPath: string) => {
    setState((current) => ({ ...current, documentPath, error: null, notice: null }))
  }, [])
  const setStartLine = useCallback((startLine: string) => {
    setState((current) => ({ ...current, error: null, startLine }))
  }, [])
  const setEndLine = useCallback((endLine: string) => {
    setState((current) => ({ ...current, endLine, error: null }))
  }, [])
  const setReason = useCallback((reason: string) => {
    setState((current) => ({ ...current, error: null, reason }))
  }, [])

  return {
    cancel: flight.cancel,
    connectVault: vault.connectVault,
    link: references.link,
    preview: references.preview,
    readReference: saved.readReference,
    reload: vault.reload,
    selectVault: vault.selectVault,
    setDocumentPath,
    setEndLine,
    setReason,
    setSearchQuery: search.setSearchQuery,
    setStartLine,
    searchDocuments: search.search,
    selectSearchMatch: search.selectMatch,
    state,
    unlink: saved.unlink,
  }
}
