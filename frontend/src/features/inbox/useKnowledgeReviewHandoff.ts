import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import type { KnowledgeImportEnvelope } from '../../domain/knowledgeImport'
import { knowledgeImportFrozen, type KnowledgeImportStatus } from './KnowledgeCaptureImportForm'

/**
 * Ephemeral execute→review handoff. Bound to the current workspace, never persisted,
 * and refused while an import is already pending or frozen for identical retry.
 */
export function useKnowledgeReviewHandoff({
  importOpen,
  importPending,
  setImportOpen,
  workspaceUid,
}: {
  importOpen: boolean
  importPending: boolean
  setImportOpen: Dispatch<SetStateAction<boolean>>
  workspaceUid: string | undefined
}) {
  const [held, setHeld] = useState<{ envelope: KnowledgeImportEnvelope; workspaceUid: string } | null>(null)
  const busyRef = useRef(false)
  const heldRef = useRef(held)
  const importPendingRef = useRef(importPending)
  heldRef.current = held
  importPendingRef.current = importPending

  useEffect(() => {
    if (importOpen) return
    // A closed import holds nothing: the dialog resets its own frozen state
    // without reporting idle upstream, so an abandoned unknown outcome would
    // otherwise keep this guard held and silently drop every later handoff.
    busyRef.current = false
    setHeld(null)
  }, [importOpen])

  useEffect(() => {
    const current = heldRef.current
    if (!current || current.workspaceUid === workspaceUid) return
    setHeld(null)
    if (!busyRef.current && !importPendingRef.current) {
      setImportOpen(false)
    }
  }, [setImportOpen, workspaceUid])

  return {
    onKnowledgeStatusChange: (status: KnowledgeImportStatus) => {
      busyRef.current = knowledgeImportFrozen(status)
    },
    onReviewKnowledge: (envelope: KnowledgeImportEnvelope) => {
      if (!workspaceUid || importPending || busyRef.current) return
      setHeld({ envelope, workspaceUid })
      setImportOpen(true)
    },
    prefill: held?.envelope ?? null,
  }
}
