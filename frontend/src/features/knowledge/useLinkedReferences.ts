import { useCallback, useEffect, useRef, useState } from 'react'

import { isKnowledgeCancelled, knowledgeErrorMessage } from './knowledgeErrors'
import { knowledgeHostAvailable, requestKnowledge } from './knowledgeHostBridge'
import { currentKnowledgeBinding } from './knowledgeSession'
import type { KnowledgeSavedReference, KnowledgeTaskRef, KnowledgeVault } from './knowledgeTypes'

const STATUS_TIMEOUT_MS = 15_000

export interface LinkedReferencesState {
  error: string | null
  hostAvailable: boolean
  loading: boolean
  references: KnowledgeSavedReference[]
  vaults: KnowledgeVault[]
}

function emptyState(hostAvailable: boolean): LinkedReferencesState {
  return { error: null, hostAvailable, loading: hostAvailable, references: [], vaults: [] }
}

/**
 * Read-only linked-reference facts for the compact Task summary. It performs one catalog
 * read per (workspace, Task) binding, never writes, and never reaches a search or model
 * path: opening a Task must not cost more than listing what is already saved.
 */
export function useLinkedReferences(workspaceUid: string, task: KnowledgeTaskRef) {
  // Seeded from the real bridge: `emptyState(false)` would first-paint the
  // "open this in the desktop app" line inside the desktop app, and only correct it
  // after the effect runs.
  const [state, setState] = useState<LinkedReferencesState>(() => emptyState(knowledgeHostAvailable()))
  const generation = useRef(0)
  const abortRef = useRef<AbortController | null>(null)
  const bindingKey = `${workspaceUid}:${task.uid}:${task.id}:${task.revision}`

  const load = useCallback(() => {
    abortRef.current?.abort()
    const available = knowledgeHostAvailable()
    const token = ++generation.current
    setState(emptyState(available))
    if (!available) return
    const controller = new AbortController()
    abortRef.current = controller
    const binding = currentKnowledgeBinding(workspaceUid, task)
    void Promise.all([
      requestKnowledge('status', {}, STATUS_TIMEOUT_MS, controller.signal),
      requestKnowledge('list-references', { binding }, STATUS_TIMEOUT_MS, controller.signal),
    ]).then(
      ([status, listed]) => {
        if (token !== generation.current) return
        setState({
          error: null,
          hostAvailable: true,
          loading: false,
          references: listed.references,
          vaults: status.vaults,
        })
      },
      (error) => {
        if (token !== generation.current || isKnowledgeCancelled(error)) return
        setState({
          error: knowledgeErrorMessage(error),
          hostAvailable: true,
          loading: false,
          references: [],
          vaults: [],
        })
      },
    )
  }, [bindingKey])

  useEffect(() => {
    load()
    return () => {
      abortRef.current?.abort()
      generation.current += 1
    }
  }, [load])

  return { reload: load, state }
}
