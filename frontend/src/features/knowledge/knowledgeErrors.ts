export class KnowledgeHostError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = 'KnowledgeHostError'
    this.code = code
  }
}

export function isKnowledgeCancelled(error: unknown) {
  return error instanceof KnowledgeHostError && error.code === 'cancelled'
}

export function isKnowledgeSourceConflict(error: unknown) {
  return error instanceof KnowledgeHostError && error.code === 'source_revision_conflict'
}

const AMBIGUOUS_PIN_CODES = new Set([
  'host_unavailable',
  'internal_error',
  'invalid_response',
  'operation_failed',
  'response_too_large',
  'timeout',
])

export function isAmbiguousKnowledgePin(error: unknown) {
  if (!(error instanceof KnowledgeHostError)) return true
  return AMBIGUOUS_PIN_CODES.has(error.code)
}

const SEARCH_ERROR_COPY: Record<string, string> = {
  search_unconfigured: 'Related-document search is not configured on this device. Use the document path below.',
  search_unavailable: 'Related-document search is unavailable. Try again or use the document path below.',
  search_timeout: 'Search timed out. Try again or use the document path below.',
  search_invalid_response: 'Search returned an invalid result. Try again or use the document path below.',
  invalid_search_query: 'Enter a nonempty search query of at most 1,000 characters, without control characters.',
}

export function knowledgeErrorMessage(error: unknown) {
  if (error instanceof KnowledgeHostError) {
    return SEARCH_ERROR_COPY[error.code] ?? error.message ?? 'The local knowledge registry could not complete that request.'
  }
  return 'The local knowledge registry could not complete that request.'
}
