import {
  DEFAULT_KNOWLEDGE_END_LINE,
  DEFAULT_KNOWLEDGE_START_LINE,
  MAX_KNOWLEDGE_EXCERPT_LINES,
  MAX_KNOWLEDGE_SEARCH_MATCHES,
  knowledgeBindingFor,
  knowledgeSearchQuerySchema,
  type KnowledgeBinding,
  type KnowledgeReadReference,
  type KnowledgeSavedReference,
  type KnowledgeSearchCorpus,
  type KnowledgeSearchData,
  type KnowledgeTaskRef,
  type KnowledgeVault,
} from './knowledgeTypes'
import { KnowledgeHostError, knowledgeErrorMessage } from './knowledgeErrors'
import { LOCAL_MARKDOWN_SOURCE } from './knowledgeSourceView'

export const LOCAL_KNOWLEDGE_COPY = LOCAL_MARKDOWN_SOURCE.scopeNote

export type KnowledgePending =
  | 'load'
  | 'choose-vault'
  | 'preview'
  | 'pin'
  | 'read'
  | 'unpin'
  | 'search'

export interface KnowledgeSearchState {
  corpus: KnowledgeSearchCorpus
  matches: KnowledgeReadReference[]
  omittedCount: number
  query: string
  vaultId: string
}

export type KnowledgeErrorAction = 'reload' | 'refresh-preview' | null

export interface KnowledgePanelState {
  documentPath: string
  endLine: string
  error: string | null
  errorAction: KnowledgeErrorAction
  errorCode: string | null
  hostAvailable: boolean
  loading: boolean
  localOnly: boolean
  notice: string | null
  opened: { read: KnowledgeReadReference; referenceId: string } | null
  pending: KnowledgePending | null
  preview: KnowledgeReadReference | null
  reason: string
  references: KnowledgeSavedReference[]
  search: KnowledgeSearchState | null
  searchQuery: string
  selectedVaultId: string
  startLine: string
  vaults: KnowledgeVault[]
}

export function emptyKnowledgeForm() {
  return {
    documentPath: '',
    endLine: '',
    opened: null as KnowledgePanelState['opened'],
    preview: null as KnowledgeReadReference | null,
    reason: '',
    search: null as KnowledgeSearchState | null,
    startLine: '',
  }
}

export function initialKnowledgeState(): KnowledgePanelState {
  return {
    ...emptyKnowledgeForm(),
    error: null,
    errorAction: null,
    errorCode: null,
    hostAvailable: false,
    loading: false,
    localOnly: true,
    notice: null,
    pending: null,
    references: [],
    searchQuery: '',
    selectedVaultId: '',
    vaults: [],
  }
}

export function parseKnowledgeLineSpan(startText: string, endText: string) {
  const start = startText.trim() === '' ? DEFAULT_KNOWLEDGE_START_LINE : Number(startText)
  const end = endText.trim() === '' ? DEFAULT_KNOWLEDGE_END_LINE : Number(endText)
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1) {
    throw new KnowledgeHostError('invalid_line_range', 'Line numbers must be whole numbers starting at 1.')
  }
  if (end < start || end - start >= MAX_KNOWLEDGE_EXCERPT_LINES) {
    throw new KnowledgeHostError('invalid_line_range', 'Use a one-based span of at most 80 lines.')
  }
  return { start_line: start, end_line: end }
}

export function previewMatchesInput(
  preview: KnowledgeReadReference | null,
  vaultId: string,
  documentPath: string,
  startLine: string,
  endLine: string,
): preview is KnowledgeReadReference {
  if (!preview || preview.vault_id !== vaultId || preview.document_path !== documentPath.trim()) return false
  try {
    const span = parseKnowledgeLineSpan(startLine, endLine)
    return preview.start_line === span.start_line && preview.end_line === span.end_line
  } catch {
    return false
  }
}

export function freshnessTone(freshness: KnowledgeReadReference['freshness']) {
  if (freshness === 'changed') return 'warning'
  if (freshness === 'unchanged') return 'verified'
  return 'unknown'
}

export function freshnessLabel(freshness: KnowledgeReadReference['freshness']) {
  if (freshness === 'changed') return 'Source changed'
  if (freshness === 'unchanged') return 'Unchanged source'
  return 'Source not compared'
}

export function selectVaultId(vaults: KnowledgeVault[], preferred: string) {
  if (preferred && vaults.some((vault) => vault.vault_id === preferred)) return preferred
  return vaults[0]?.vault_id ?? ''
}

export function currentKnowledgeBinding(workspaceUid: string, task: KnowledgeTaskRef): KnowledgeBinding {
  return knowledgeBindingFor(workspaceUid, task)
}

export function vaultLabel(vaults: KnowledgeVault[], vaultId: string) {
  return vaults.find((vault) => vault.vault_id === vaultId)?.label ?? vaultId
}

function errorIdentity(error: unknown) {
  return {
    error: knowledgeErrorMessage(error),
    errorCode: error instanceof KnowledgeHostError ? error.code : 'unknown',
  }
}

export function failState(current: KnowledgePanelState, error: unknown): KnowledgePanelState {
  const searchPending = current.pending === 'search'
  return {
    ...current,
    ...errorIdentity(error),
    errorAction: searchPending ? null : 'reload',
    loading: false,
    pending: null,
    search: searchPending ? null : current.search,
  }
}

export function normalizeKnowledgeSearchQuery(value: string) {
  const parsed = knowledgeSearchQuerySchema.safeParse(value.trim())
  if (!parsed.success) {
    throw new KnowledgeHostError(
      'invalid_search_query',
      'Enter a nonempty search query of at most 1,000 characters, without control characters.',
    )
  }
  return parsed.data
}

export function applySearchResult(
  current: KnowledgePanelState,
  data: KnowledgeSearchData,
  sentQuery: string,
  sentVaultId: string,
): KnowledgePanelState {
  if (current.selectedVaultId !== sentVaultId) return { ...current, pending: null }
  if (current.searchQuery.trim() !== sentQuery) return { ...current, pending: null }
  if (data.query !== sentQuery) return { ...current, pending: null }
  const matches: KnowledgeReadReference[] = []
  let omittedCount = data.omitted_count
  for (const match of data.matches) {
    if (match.vault_id !== sentVaultId || match.freshness !== 'unchanged' || matches.length >= MAX_KNOWLEDGE_SEARCH_MATCHES) {
      omittedCount += 1
      continue
    }
    matches.push(match)
  }
  return {
    ...current,
    error: null,
    errorAction: null,
    errorCode: null,
    pending: null,
    search: {
      corpus: data.corpus,
      matches,
      omittedCount,
      query: data.query,
      vaultId: sentVaultId,
    },
  }
}

export function applySearchMatch(current: KnowledgePanelState, match: KnowledgeReadReference): KnowledgePanelState {
  if (match.vault_id !== current.selectedVaultId) return current
  const changed = match.freshness === 'changed'
  return {
    ...current,
    documentPath: match.document_path,
    endLine: String(match.end_line),
    error: changed ? 'The selected source changed. Preview the current span before linking.' : null,
    errorAction: changed ? 'refresh-preview' : null,
    errorCode: changed ? 'source_revision_conflict' : null,
    notice: null,
    opened: null,
    preview: changed ? null : match,
    startLine: String(match.start_line),
  }
}

export function conflictPinState(current: KnowledgePanelState, error: unknown): KnowledgePanelState {
  return {
    ...failState(current, error),
    errorAction: 'refresh-preview',
    preview: null,
  }
}

export function reconcileAfterAmbiguousPin(
  current: KnowledgePanelState,
  references: KnowledgeSavedReference[],
  error: unknown,
): KnowledgePanelState {
  return {
    ...current,
    ...errorIdentity(error),
    errorAction: null,
    loading: false,
    pending: null,
    references,
  }
}

export function applyCatalog(
  current: KnowledgePanelState,
  vaults: KnowledgeVault[],
  references: KnowledgeSavedReference[],
): KnowledgePanelState {
  return {
    ...current,
    error: null,
    errorAction: null,
    errorCode: null,
    hostAvailable: true,
    loading: false,
    localOnly: true,
    pending: null,
    references,
    selectedVaultId: selectVaultId(vaults, current.selectedVaultId),
    vaults,
  }
}
