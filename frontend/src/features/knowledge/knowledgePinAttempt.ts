import { requestKnowledge } from './knowledgeHostBridge'
import { isAmbiguousKnowledgePin, isKnowledgeCancelled, isKnowledgeSourceConflict } from './knowledgeErrors'
import type {
  KnowledgeBinding,
  KnowledgeListData,
  KnowledgePinData,
  KnowledgeReadReference,
} from './knowledgeTypes'

/**
 * What one pin request settled as. A source conflict and an ambiguous pin are
 * answers about the vault, so they are returned; every other error - including
 * cancellation - propagates to the caller's flight guard.
 */
export type KnowledgePinAttempt =
  | { status: 'pinned'; data: KnowledgePinData }
  | { status: 'conflict'; error: unknown }
  | { status: 'reconciled'; error: unknown; listed: KnowledgeListData }

export async function attemptKnowledgePin(
  binding: KnowledgeBinding,
  preview: KnowledgeReadReference,
  reason: string,
  timeoutMs: number,
  signal: AbortSignal,
): Promise<KnowledgePinAttempt> {
  try {
    return {
      status: 'pinned',
      data: await requestKnowledge('pin-reference', {
        binding,
        document_path: preview.document_path,
        end_line: preview.end_line,
        expected_sha256: preview.source_sha256,
        reason: reason.trim(),
        start_line: preview.start_line,
        vault_id: preview.vault_id,
      }, timeoutMs, signal),
    }
  } catch (error) {
    if (isKnowledgeCancelled(error)) throw error
    if (isKnowledgeSourceConflict(error)) return { status: 'conflict', error }
    if (!isAmbiguousKnowledgePin(error)) throw error
    try {
      const listed = await requestKnowledge('list-references', { binding }, timeoutMs, signal)
      return { status: 'reconciled', error, listed }
    } catch (listedError) {
      if (isKnowledgeCancelled(listedError)) throw listedError
      throw error
    }
  }
}
