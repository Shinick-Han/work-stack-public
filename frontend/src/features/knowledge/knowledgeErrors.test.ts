import { expect, test } from 'vitest'

import {
  KnowledgeHostError,
  isAmbiguousKnowledgePin,
  isKnowledgeCancelled,
  isKnowledgeSourceConflict,
  knowledgeErrorMessage,
} from './knowledgeErrors'

const GENERIC = 'The local knowledge registry could not complete that request.'

test('known search codes keep their exact device-local copy', () => {
  expect(knowledgeErrorMessage(new KnowledgeHostError('search_timeout', 'raw host text')))
    .toBe('Search timed out. Try again or use the document path below.')
  expect(knowledgeErrorMessage(new KnowledgeHostError('search_unconfigured', 'raw host text')))
    .toBe('Related-document search is not configured on this device. Use the document path below.')
  expect(knowledgeErrorMessage(new KnowledgeHostError('search_unavailable', 'raw host text')))
    .toBe('Related-document search is unavailable. Try again or use the document path below.')
  expect(knowledgeErrorMessage(new KnowledgeHostError('search_invalid_response', 'raw host text')))
    .toBe('Search returned an invalid result. Try again or use the document path below.')
  expect(knowledgeErrorMessage(new KnowledgeHostError('invalid_search_query', 'raw host text')))
    .toBe('Enter a nonempty search query of at most 1,000 characters, without control characters.')
})

test('inherited prototype names admitted by the wire schema fall back to the host message', () => {
  // Both codes satisfy the closed /^[a-z0-9_]{1,64}$/ shape the error envelope admits.
  for (const code of ['constructor', '__proto__', 'valueof', 'hasownproperty']) {
    const message = knowledgeErrorMessage(new KnowledgeHostError(code, 'The vault index is rebuilding.'))
    expect(typeof message).toBe('string')
    expect(message).toBe('The vault index is rebuilding.')
  }
})

test('an ordinary unknown host code still shows the admitted host message, not HTTP-style text', () => {
  expect(knowledgeErrorMessage(new KnowledgeHostError('registry_unavailable', 'Local knowledge registry is unavailable.')))
    .toBe('Local knowledge registry is unavailable.')
  expect(knowledgeErrorMessage(new KnowledgeHostError('host_unavailable', 'The desktop knowledge host is not available.')))
    .toBe('The desktop knowledge host is not available.')
})

test('a host error without usable text and any non-host error keep the generic literal', () => {
  expect(knowledgeErrorMessage(new KnowledgeHostError('constructor', ''))).toBe(GENERIC)
  expect(knowledgeErrorMessage(new Error('boom'))).toBe(GENERIC)
  expect(knowledgeErrorMessage('constructor')).toBe(GENERIC)
  expect(knowledgeErrorMessage(null)).toBe(GENERIC)
  expect(knowledgeErrorMessage(undefined)).toBe(GENERIC)
  expect(knowledgeErrorMessage({ code: 'search_timeout', message: 'not a host error' })).toBe(GENERIC)
})

test('the cancellation, source-conflict and ambiguous-pin classifiers are unchanged', () => {
  expect(isKnowledgeCancelled(new KnowledgeHostError('cancelled', 'cancelled'))).toBe(true)
  expect(isKnowledgeCancelled(new KnowledgeHostError('timeout', 'timed out'))).toBe(false)
  expect(isKnowledgeCancelled(new Error('cancelled'))).toBe(false)

  expect(isKnowledgeSourceConflict(new KnowledgeHostError('source_revision_conflict', 'changed'))).toBe(true)
  expect(isKnowledgeSourceConflict(new KnowledgeHostError('cancelled', 'cancelled'))).toBe(false)

  for (const code of ['host_unavailable', 'internal_error', 'invalid_response', 'operation_failed', 'response_too_large', 'timeout']) {
    expect(isAmbiguousKnowledgePin(new KnowledgeHostError(code, 'ambiguous'))).toBe(true)
  }
  expect(isAmbiguousKnowledgePin(new KnowledgeHostError('source_revision_conflict', 'changed'))).toBe(false)
  expect(isAmbiguousKnowledgePin(new KnowledgeHostError('constructor', 'inherited name'))).toBe(false)
  expect(isAmbiguousKnowledgePin(new Error('unknown shape'))).toBe(true)
})
