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

const GENERIC_KNOWLEDGE_ERROR = 'The local knowledge registry could not complete that request.'

const SEARCH_ERROR_COPY: Record<string, string> = {
  search_unconfigured: 'Related-document search is not configured on this device. Use the document path below.',
  search_unavailable: 'Related-document search is unavailable. Try again or use the document path below.',
  search_timeout: 'Search timed out. Try again or use the document path below.',
  search_invalid_response: 'Search returned an invalid result. Try again or use the document path below.',
  invalid_search_query: 'Enter a nonempty search query of at most 1,000 characters, without control characters.',
}

/**
 * The wire schema admits any `/^[a-z0-9_]{1,64}$/` code, which includes inherited
 * `Object.prototype` names such as `constructor` and `__proto__`. A bare index into the
 * copy table would hand those an inherited function or object instead of missing, so the
 * table only answers for an own string entry.
 */
function knownSearchCopy(code: string) {
  if (!Object.prototype.hasOwnProperty.call(SEARCH_ERROR_COPY, code)) return undefined
  const copy = SEARCH_ERROR_COPY[code]
  return typeof copy === 'string' ? copy : undefined
}

export function knowledgeErrorMessage(error: unknown): string {
  if (error instanceof KnowledgeHostError) {
    const copy = typeof error.code === 'string' ? knownSearchCopy(error.code) : undefined
    if (copy !== undefined) return copy
    // The host message the bridge already admitted (bounded, control-character free)
    // stays ahead of the generic literal: it names the device-local failure.
    return typeof error.message === 'string' && error.message ? error.message : GENERIC_KNOWLEDGE_ERROR
  }
  return GENERIC_KNOWLEDGE_ERROR
}
