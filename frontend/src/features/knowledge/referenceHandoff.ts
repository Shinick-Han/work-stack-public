import { KnowledgeHostError } from './knowledgeErrors'
import { maybeResumeBriefMarkdown } from './referenceBrief'
import {
  knowledgeBindingFor,
  type KnowledgeBinding,
  type KnowledgeReadReference,
  type KnowledgeSavedReference,
  type KnowledgeTaskRef,
  type KnowledgeVault,
} from './knowledgeTypes'
import {
  resumeProgressKey,
  type ResumeProgressFacts,
} from './resumeProgressContract'

const KNOWLEDGE_CONTEXT_SCHEMA = 'workstack.knowledge-context.v1' as const
const MAX_HANDOFF_SELECTION = 8
const MAX_HANDOFF_BYTES = 32 * 1024

export interface KnowledgeContextEnvelope {
  schema: typeof KNOWLEDGE_CONTEXT_SCHEMA
  binding: KnowledgeBinding
  references: KnowledgeReadReference[]
  generated: false
}

export interface LiveTaskSnapshot {
  id: string
  uid: string
  revision: number
  title: string
  detail: string
  status: string
}

export interface PreparedReferenceHandoff {
  briefMarkdown: string | null
  changedIds: string[]
  envelope: KnowledgeContextEnvelope
  json: string
  /** The recorded-progress facts frozen into this brief, never re-read afterwards. */
  progress: ResumeProgressFacts
  /** Identity of those facts, so a checkpoint-only change marks this brief stale. */
  progressKey: string
  referenceIds: string[]
}

export class ReferenceHandoffError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'ReferenceHandoffError'
    this.code = code
  }
}

export function handoffBinding(workspaceUid: string, task: KnowledgeTaskRef) {
  return knowledgeBindingFor(workspaceUid, task)
}

export function nextHandoffSelection(selected: readonly string[], referenceId: string) {
  if (selected.includes(referenceId)) return selected.filter((id) => id !== referenceId)
  if (selected.length >= MAX_HANDOFF_SELECTION) return [...selected]
  return [...selected, referenceId]
}

export function selectionAtLimit(selected: readonly string[]) {
  return selected.length >= MAX_HANDOFF_SELECTION
}

export function exportedReadReference(read: KnowledgeReadReference): KnowledgeReadReference {
  return {
    schema_version: 1,
    provider: 'markdown-vault',
    vault_id: read.vault_id,
    document_path: read.document_path,
    title: read.title,
    source_sha256: read.source_sha256,
    expected_sha256: read.expected_sha256,
    freshness: read.freshness,
    start_line: read.start_line,
    end_line: read.end_line,
    excerpt: read.excerpt,
    excerpt_truncated: read.excerpt_truncated,
    trust: 'external_reference',
    read_only: true,
  }
}

export function buildKnowledgeContext(
  binding: KnowledgeBinding,
  reads: KnowledgeReadReference[],
): KnowledgeContextEnvelope {
  return {
    schema: KNOWLEDGE_CONTEXT_SCHEMA,
    binding: {
      workspace_uid: binding.workspace_uid,
      task_uid: binding.task_uid,
      task_id: binding.task_id,
      task_revision: binding.task_revision,
    },
    references: reads.map(exportedReadReference),
    generated: false,
  }
}

export function serializeKnowledgeContext(envelope: KnowledgeContextEnvelope) {
  return JSON.stringify(envelope, null, 2)
}

export function knowledgeContextUtf8Bytes(serialized: string) {
  return new TextEncoder().encode(serialized).byteLength
}

export function assertHandoffEnvelopeFits(serialized: string) {
  if (knowledgeContextUtf8Bytes(serialized) > MAX_HANDOFF_BYTES) {
    throw new ReferenceHandoffError(
      'envelope_too_large',
      'The prepared JSON exceeds 32KiB. Select fewer references. Nothing was exported.',
    )
  }
}

export function knowledgeContextFilename(binding: KnowledgeBinding) {
  const taskId = binding.task_id.replace(/[^A-Za-z0-9._-]+/g, '_')
  return `workstack-knowledge-context-${taskId}-r${binding.task_revision}.json`
}

export function liveTaskMatchesBinding(
  binding: KnowledgeBinding,
  live: LiveTaskSnapshot,
  workspaceUid: string,
) {
  return workspaceUid === binding.workspace_uid
    && live.id === binding.task_id
    && live.uid === binding.task_uid
    && live.revision === binding.task_revision
}

export function staleBindingError() {
  return new ReferenceHandoffError(
    'binding_stale',
    'The live Task no longer matches this selected revision. Binding uses the selected Task revision, not unsaved draft content.',
  )
}

export function pairedReadMatchesSelection(
  saved: KnowledgeSavedReference,
  read: KnowledgeReadReference,
) {
  return read.vault_id === saved.vault_id
    && read.document_path === saved.document_path
    && read.start_line === saved.start_line
    && read.end_line === saved.end_line
    && read.expected_sha256 === saved.source_sha256
    && read.trust === 'external_reference'
    && read.read_only === true
    && read.provider === 'markdown-vault'
    && read.schema_version === 1
}

export function pinnedReadComparisonValid(read: KnowledgeReadReference) {
  if (read.freshness === 'uncompared' || read.expected_sha256 === null) return false
  if (read.freshness === 'unchanged') return read.source_sha256 === read.expected_sha256
  return read.source_sha256 !== read.expected_sha256
}

export function admitPreparedRead(saved: KnowledgeSavedReference, read: KnowledgeReadReference) {
  if (!pairedReadMatchesSelection(saved, read)) {
    throw new ReferenceHandoffError(
      'read_mismatch',
      'A selected reference no longer matches the prepared read. Nothing was exported.',
    )
  }
  if (!pinnedReadComparisonValid(read)) {
    throw new ReferenceHandoffError(
      'freshness_invalid',
      'A pinned reference was not compared to its saved hash. Nothing was exported.',
    )
  }
  return exportedReadReference(read)
}

export function selectedSavedReferences(
  references: readonly KnowledgeSavedReference[],
  selectedIds: readonly string[],
) {
  const byId = new Map(references.map((item) => [item.reference_id, item]))
  const selected: KnowledgeSavedReference[] = []
  for (const id of selectedIds) {
    const item = byId.get(id)
    if (!item) {
      throw new ReferenceHandoffError(
        'selection_missing',
        'A selected reference is no longer in the saved list. Nothing was exported.',
      )
    }
    selected.push(item)
  }
  if (selected.length === 0) {
    throw new ReferenceHandoffError(
      'selection_required',
      'Select at least one linked reference, at most eight. Nothing was exported.',
    )
  }
  if (selected.length > MAX_HANDOFF_SELECTION) {
    throw new ReferenceHandoffError(
      'selection_limit',
      'Select at most eight linked references. Nothing was exported.',
    )
  }
  return selected
}

export function sameSavedReferenceRecord(
  left: KnowledgeSavedReference,
  right: KnowledgeSavedReference,
) {
  return left.reference_id === right.reference_id
    && left.vault_id === right.vault_id
    && left.document_path === right.document_path
    && left.start_line === right.start_line
    && left.end_line === right.end_line
    && left.source_sha256 === right.source_sha256
    && left.reason === right.reason
}

/**
 * Resolves the selection the user made against the saved references the registry
 * reports right now. A selected id that disappeared, or whose vault, path, span,
 * pinned hash or reason moved, refuses the whole preparation: the user reloads the
 * list and selects again rather than having the selection silently repointed at a
 * record they never saw.
 */
export function currentSelectedReferences(
  listed: readonly KnowledgeSavedReference[],
  current: readonly KnowledgeSavedReference[],
  selectedIds: readonly string[],
) {
  const selected = selectedSavedReferences(listed, selectedIds)
  const byId = new Map(current.map((item) => [item.reference_id, item]))
  return selected.map((saved) => {
    const now = byId.get(saved.reference_id)
    if (!now) {
      throw new ReferenceHandoffError(
        'selection_removed',
        'A selected reference is no longer saved for this Task. Reload the linked references and select again. Nothing was exported.',
      )
    }
    if (!sameSavedReferenceRecord(saved, now)) {
      throw new ReferenceHandoffError(
        'selection_changed',
        'A selected reference changed since it was listed. Reload the linked references and select again. Nothing was exported.',
      )
    }
    return now
  })
}

export function changedReferenceIds(
  selected: readonly KnowledgeSavedReference[],
  reads: readonly KnowledgeReadReference[],
) {
  return selected
    .filter((saved, index) => reads[index]?.freshness === 'changed')
    .map((saved) => saved.reference_id)
}

export function allChangedAcknowledged(changedIds: readonly string[], acknowledged: readonly string[]) {
  return changedIds.every((id) => acknowledged.includes(id))
}

export async function assertLiveTaskBinding(
  binding: KnowledgeBinding,
  readLiveTask: (taskId: string) => Promise<LiveTaskSnapshot>,
  readWorkspaceUid: () => Promise<string>,
) {
  const [live, workspaceUid] = await Promise.all([
    readLiveTask(binding.task_id),
    readWorkspaceUid(),
  ])
  if (!liveTaskMatchesBinding(binding, live, workspaceUid)) throw staleBindingError()
  return live
}

export async function prepareReferenceHandoff(input: {
  binding: KnowledgeBinding
  listReferences: (signal: AbortSignal) => Promise<readonly KnowledgeSavedReference[]>
  progress: ResumeProgressFacts
  readLiveTask: (taskId: string) => Promise<LiveTaskSnapshot>
  readReference: (saved: KnowledgeSavedReference, signal: AbortSignal) => Promise<KnowledgeReadReference>
  readWorkspaceUid: () => Promise<string>
  references: readonly KnowledgeSavedReference[]
  selectedIds: readonly string[]
  signal: AbortSignal
  vaults: readonly KnowledgeVault[]
}): Promise<PreparedReferenceHandoff> {
  selectedSavedReferences(input.references, input.selectedIds)
  await assertLiveTaskBinding(input.binding, input.readLiveTask, input.readWorkspaceUid)
  if (input.signal.aborted) throw new KnowledgeHostError('cancelled', 'The knowledge request was cancelled.')
  const selected = currentSelectedReferences(
    input.references,
    await input.listReferences(input.signal),
    input.selectedIds,
  )
  const reads: KnowledgeReadReference[] = []
  for (const saved of selected) {
    if (input.signal.aborted) throw new KnowledgeHostError('cancelled', 'The knowledge request was cancelled.')
    reads.push(admitPreparedRead(saved, await input.readReference(saved, input.signal)))
  }
  const live = await assertLiveTaskBinding(input.binding, input.readLiveTask, input.readWorkspaceUid)
  const envelope = buildKnowledgeContext(input.binding, reads)
  const json = serializeKnowledgeContext(envelope)
  assertHandoffEnvelopeFits(json)
  return {
    briefMarkdown: maybeResumeBriefMarkdown({
      binding: input.binding,
      progress: input.progress,
      reads,
      saved: selected,
      task: live,
      vaults: input.vaults,
    }),
    changedIds: changedReferenceIds(selected, reads),
    envelope,
    json,
    progress: input.progress,
    progressKey: resumeProgressKey(input.progress),
    referenceIds: selected.map((item) => item.reference_id),
  }
}

export function downloadKnowledgeContextJson(filename: string, json: string) {
  const blob = new Blob([json], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.hidden = true
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
