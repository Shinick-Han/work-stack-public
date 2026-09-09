import { expect, test } from 'vitest'
import {
  KNOWLEDGE_CAPTURE_IMPORT_MESSAGES,
  KnowledgeCaptureImportError,
  describeKnowledgeCaptureImportError,
} from './schemaKnowledgeCapture'

const IMPORT_REFUSED = KNOWLEDGE_CAPTURE_IMPORT_MESSAGES.import_refused

test('known import codes keep their authored copy', () => {
  expect(describeKnowledgeCaptureImportError('request_expired')).toBe(
    KNOWLEDGE_CAPTURE_IMPORT_MESSAGES.request_expired,
  )
  expect(describeKnowledgeCaptureImportError('import_pending')).toBe(
    KNOWLEDGE_CAPTURE_IMPORT_MESSAGES.import_pending,
  )
  expect(describeKnowledgeCaptureImportError('invalid_json')).toBe(
    KNOWLEDGE_CAPTURE_IMPORT_MESSAGES.invalid_json,
  )
  expect(describeKnowledgeCaptureImportError('import_refused')).toBe(IMPORT_REFUSED)
  expect(typeof IMPORT_REFUSED).toBe('string')
})

test('unknown codes and inherited Object.prototype names return the import_refused string', () => {
  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty', 'valueOf', 'not_a_published_code']) {
    const message = describeKnowledgeCaptureImportError(code)
    expect(message).toBe(IMPORT_REFUSED)
    expect(typeof message).toBe('string')
    expect(new KnowledgeCaptureImportError(code).message).toBe(IMPORT_REFUSED)
    expect(new KnowledgeCaptureImportError(code).code).toBe(code)
  }
})
