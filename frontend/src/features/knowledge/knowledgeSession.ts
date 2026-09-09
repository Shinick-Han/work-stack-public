import {
  DEFAULT_KNOWLEDGE_END_LINE,
  DEFAULT_KNOWLEDGE_START_LINE,
  MAX_KNOWLEDGE_EXCERPT_LINES,
  MAX_KNOWLEDGE_SEARCH_MATCHES,
  knowledgeBindingFor,
  knowledgeSearchQuerySchema,
  sameKnowledgeBinding,
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
  binding: KnowledgeBinding | null
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
    binding: null,
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

export interface KnowledgePreviewRequest {
  binding: KnowledgeBinding
  documentPath: string
  endLine: number
  startLine: number
  vaultId: string
}

/** The vault/path/span half of a request, without the Task snapshot it was read under. */
export type KnowledgePreviewSpanRequest = Omit<KnowledgePreviewRequest, 'binding'>

const boundPreviewRequests = new WeakMap<KnowledgeReadReference, KnowledgePreviewRequest>()

export function bindKnowledgePreview(
  reference: KnowledgeReadReference,
  request: KnowledgePreviewRequest,
): KnowledgeReadReference {
  boundPreviewRequests.set(reference, request)
  return reference
}

export function knowledgePreviewRequest(preview: KnowledgeReadReference) {
  return boundPreviewRequests.get(preview)
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

function formMatchesPreviewRequest(
  vaultId: string,
  documentPath: string,
  startLine: string,
  endLine: string,
  request: KnowledgePreviewSpanRequest,
) {
  if (request.vaultId !== vaultId || request.documentPath !== documentPath.trim()) return false
  try {
    const span = parseKnowledgeLineSpan(startLine, endLine)
    return span.start_line === request.startLine && span.end_line === request.endLine
  } catch {
    return false
  }
}

export function previewSpanMatchesRequest(
  preview: KnowledgeReadReference,
  request: KnowledgePreviewSpanRequest,
) {
  return preview.vault_id === request.vaultId
    && preview.document_path === request.documentPath
    && preview.start_line === request.startLine
    && preview.end_line >= request.startLine
    && preview.end_line <= request.endLine
}

export function previewBindingMatches(
  request: KnowledgePreviewRequest,
  currentBinding: KnowledgeBinding | null,
) {
  return currentBinding !== null && sameKnowledgeBinding(request.binding, currentBinding)
}

export function previewMatchesInput(
  preview: KnowledgeReadReference | null,
  vaultId: string,
  documentPath: string,
  startLine: string,
  endLine: string,
  currentBinding?: KnowledgeBinding | null,
): preview is KnowledgeReadReference {
  if (!preview || preview.vault_id !== vaultId || preview.document_path !== documentPath.trim()) return false
  const request = boundPreviewRequests.get(preview)
  if (!request) {
    // An unbound preview carries no Task snapshot, so it can never authorize a pin.
    if (currentBinding !== undefined) return false
    const own: KnowledgePreviewSpanRequest = {
      documentPath: preview.document_path,
      endLine: preview.end_line,
      startLine: preview.start_line,
      vaultId: preview.vault_id,
    }
    return formMatchesPreviewRequest(vaultId, documentPath, startLine, endLine, own)
      && previewSpanMatchesRequest(preview, own)
  }
  if (currentBinding !== undefined && !previewBindingMatches(request, currentBinding)) return false
  return formMatchesPreviewRequest(vaultId, documentPath, startLine, endLine, request)
    && previewSpanMatchesRequest(preview, request)
}

export function applyPreviewResult(
  current: KnowledgePanelState,
  reference: KnowledgeReadReference,
  request: KnowledgePreviewRequest,
): KnowledgePanelState {
  if (!previewBindingMatches(request, current.binding)) {
    // The Task snapshot moved on (a new revision, Task, or workspace) while this read was in flight.
    return { ...current, pending: null }
  }
  if (!formMatchesPreviewRequest(
    current.selectedVaultId,
    current.documentPath,
    current.startLine,
    current.endLine,
    request,
  )) {
    return { ...current, pending: null }
  }
  if (!previewSpanMatchesRequest(reference, request)) {
    return failState(current, new KnowledgeHostError(
      'invalid_line_range',
      'The previewed span does not match the requested document range.',
    ))
  }
  return {
    ...current,
    error: null,
    errorAction: null,
    errorCode: null,
    opened: null,
    pending: null,
    preview: bindKnowledgePreview(reference, request),
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
    preview: changed || current.binding === null
      ? null
      : bindKnowledgePreview(match, {
        binding: current.binding,
        documentPath: match.document_path,
        endLine: match.end_line,
        startLine: match.start_line,
        vaultId: match.vault_id,
      }),
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
