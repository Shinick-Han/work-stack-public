import { expect, test } from 'vitest'
import { KnowledgeCaptureImportError } from '../../domain/schemaKnowledgeCapture'
import { acceptKnowledgeExecutionProposal } from './knowledgeExecutionAccept'
import { knowledgeImportEnvelope, REQUEST_ID } from './knowledgeCaptureFixture'

test('accepts a canonical envelope at or under the issued result limit', () => {
  const envelope = knowledgeImportEnvelope()
  expect(acceptKnowledgeExecutionProposal(envelope, { request_id: REQUEST_ID, result_limit: 5 })).toEqual(envelope)
})

test('refuses a raw string, a wrong request id, and an over-limit item list', () => {
  expect(() => acceptKnowledgeExecutionProposal('driver stderr leaked', {
    request_id: REQUEST_ID,
    result_limit: 5,
  })).toThrow(KnowledgeCaptureImportError)

  expect(() => acceptKnowledgeExecutionProposal(
    knowledgeImportEnvelope({ request_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc' }),
    { request_id: REQUEST_ID, result_limit: 5 },
  )).toThrow(KnowledgeCaptureImportError)

  const item = knowledgeImportEnvelope().items[0]
  const extra = {
    ...item,
    item_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
    retrieval: { ...item.retrieval, query_id: 'engine-q-other' },
  }
  expect(() => acceptKnowledgeExecutionProposal(
    knowledgeImportEnvelope({ items: [item, extra] }),
    { request_id: REQUEST_ID, result_limit: 1 },
  )).toThrow(KnowledgeCaptureImportError)
})
