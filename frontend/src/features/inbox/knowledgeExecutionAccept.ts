import { KnowledgeCaptureImportError } from '../../domain/schemaKnowledgeCapture'
import { parseKnowledgeImportText, type KnowledgeImportEnvelope } from './knowledgeCaptureImport'

/**
 * Re-project untrusted execute `data` through the existing import parser.
 *
 * The API layer returns unknown JSON on purpose. Acceptance here is the same closed
 * envelope a person would paste, plus the issued request's identity and item limit.
 */
export function acceptKnowledgeExecutionProposal(
  data: unknown,
  issued: { request_id: string; result_limit: number },
): KnowledgeImportEnvelope {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    throw new KnowledgeCaptureImportError('invalid_request')
  }
  let text: string
  try {
    text = JSON.stringify(data)
  } catch {
    throw new KnowledgeCaptureImportError('invalid_json')
  }
  const envelope = parseKnowledgeImportText(text)
  if (envelope.request_id !== issued.request_id) {
    throw new KnowledgeCaptureImportError('request_id_mismatch')
  }
  if (envelope.items.length > issued.result_limit) {
    throw new KnowledgeCaptureImportError('result_limit_exceeded')
  }
  return envelope
}
