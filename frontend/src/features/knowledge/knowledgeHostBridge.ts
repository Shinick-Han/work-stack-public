import { KnowledgeHostError } from './knowledgeErrors'
import {
  KNOWLEDGE_SCHEMA_VERSION,
  MAX_KNOWLEDGE_REQUEST_BYTES,
  MAX_KNOWLEDGE_RESPONSE_BYTES,
  knowledgeHostRequestSchema,
  knowledgeHostResponseSchema,
  peekKnowledgeRequestId,
  sameKnowledgeBinding,
  type KnowledgeBinding,
  type KnowledgeDataByOperation,
  type KnowledgeHostResponse,
  type KnowledgeOperation,
} from './knowledgeTypes'

interface WebViewMessageEvent extends Event { data?: unknown }
interface KnowledgeHostWindow extends Window {
  chrome?: {
    webview?: {
      addEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      removeEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      postMessage: (message: string) => void
    }
  }
}

const DEFAULT_TIMEOUT_MS = 15_000

function hostWindow() {
  return window as KnowledgeHostWindow
}

export function knowledgeHostAvailable() {
  const bridge = hostWindow().chrome?.webview
  return typeof bridge?.postMessage === 'function' && typeof bridge.addEventListener === 'function'
}

export function serializedKnowledgeBytes(value: unknown) {
  try {
    const serialized = typeof value === 'string' ? value : JSON.stringify(value)
    return serialized === undefined ? null : new TextEncoder().encode(serialized).byteLength
  } catch {
    return null
  }
}

function decodeHostData(data: unknown) {
  if (typeof data === 'string') return JSON.parse(data) as unknown
  return data
}

function createRequestId() {
  return window.crypto.randomUUID()
}

function bindingFromPayload(payload: object): KnowledgeBinding | undefined {
  if (!('binding' in payload)) return undefined
  const binding = (payload as { binding?: unknown }).binding
  return binding && typeof binding === 'object' ? binding as KnowledgeBinding : undefined
}

function buildRequest(operation: KnowledgeOperation, payload: object, requestId: string) {
  return knowledgeHostRequestSchema.parse({
    type: 'workstack-knowledge-request',
    schema_version: KNOWLEDGE_SCHEMA_VERSION,
    request_id: requestId,
    operation,
    ...payload,
  })
}

function dataFromResponse<K extends KnowledgeOperation>(
  operation: K,
  expectedBinding: KnowledgeBinding | undefined,
  message: KnowledgeHostResponse,
): KnowledgeDataByOperation[K] {
  if (message.operation !== operation) {
    throw new KnowledgeHostError('operation_mismatch', 'The knowledge host replied to a different operation.')
  }
  if (!message.ok) throw new KnowledgeHostError(message.error.code, message.error.message)
  const data = message.data as KnowledgeDataByOperation[K]
  if (!expectedBinding) return data
  if (!('binding' in data) || !sameKnowledgeBinding(expectedBinding, data.binding)) {
    throw new KnowledgeHostError('binding_mismatch', 'The knowledge host reply no longer matches this Task snapshot.')
  }
  return data
}

function oversizedOrInvalid(data: unknown, requestId: string) {
  const bytes = serializedKnowledgeBytes(data)
  if (bytes !== null && bytes <= MAX_KNOWLEDGE_RESPONSE_BYTES) return undefined
  try {
    if (peekKnowledgeRequestId(decodeHostData(data)) !== requestId) return null
  } catch {
    return null
  }
  const tooLarge = bytes !== null && bytes > MAX_KNOWLEDGE_RESPONSE_BYTES
  return new KnowledgeHostError(
    tooLarge ? 'response_too_large' : 'invalid_response',
    tooLarge ? 'The knowledge host response exceeded 128KiB.' : 'The knowledge host reply could not be read.',
  )
}

function parseMatchedResponse<K extends KnowledgeOperation>(
  operation: K,
  expectedBinding: KnowledgeBinding | undefined,
  data: unknown,
): KnowledgeDataByOperation[K] {
  const decoded = decodeHostData(data)
  const parsed = knowledgeHostResponseSchema.safeParse(decoded)
  if (!parsed.success) {
    throw new KnowledgeHostError('invalid_response', 'The knowledge host reply failed bounded schema checks.')
  }
  return dataFromResponse(operation, expectedBinding, parsed.data)
}

function listenForKnowledgeResponse<K extends KnowledgeOperation>(
  operation: K,
  requestId: string,
  expectedBinding: KnowledgeBinding | undefined,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<KnowledgeDataByOperation[K]> {
  const bridge = hostWindow().chrome?.webview
  if (!bridge || typeof bridge.addEventListener !== 'function' || typeof bridge.removeEventListener !== 'function') {
    return Promise.reject(new KnowledgeHostError('host_unavailable', 'The desktop knowledge host is not available.'))
  }
  const addEventListener = bridge.addEventListener.bind(bridge)
  const removeEventListener = bridge.removeEventListener.bind(bridge)
  return new Promise((resolve, reject) => {
    let settled = false
    const finish = (error: unknown, value?: KnowledgeDataByOperation[K]) => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      removeEventListener('message', receive)
      if (error) reject(error)
      else resolve(value as KnowledgeDataByOperation[K])
    }
    const onAbort = () => finish(new KnowledgeHostError('cancelled', 'The knowledge request was cancelled.'))
    const receive = (event: WebViewMessageEvent) => {
      const sizeError = oversizedOrInvalid(event.data, requestId)
      if (sizeError === null) return
      if (sizeError) {
        finish(sizeError)
        return
      }
      let decoded: unknown
      try {
        decoded = decodeHostData(event.data)
      } catch {
        return
      }
      if (peekKnowledgeRequestId(decoded) !== requestId) return
      try {
        finish(undefined, parseMatchedResponse(operation, expectedBinding, event.data))
      } catch (error) {
        finish(error)
      }
    }
    const timer = window.setTimeout(() => {
      finish(new KnowledgeHostError('timeout', 'The knowledge host did not reply before the request timed out.'))
    }, Math.max(100, timeoutMs))
    signal?.addEventListener('abort', onAbort)
    if (signal?.aborted) {
      onAbort()
      return
    }
    addEventListener('message', receive)
  })
}

export function requestKnowledge<K extends KnowledgeOperation>(
  operation: K,
  payload: object = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
  signal?: AbortSignal,
): Promise<KnowledgeDataByOperation[K]> {
  if (signal?.aborted) {
    return Promise.reject(new KnowledgeHostError('cancelled', 'The knowledge request was cancelled.'))
  }
  if (!knowledgeHostAvailable()) {
    return Promise.reject(new KnowledgeHostError('host_unavailable', 'The desktop knowledge host is not available.'))
  }
  const request = buildRequest(operation, payload, createRequestId())
  const serialized = JSON.stringify(request)
  if (new TextEncoder().encode(serialized).byteLength > MAX_KNOWLEDGE_REQUEST_BYTES) {
    return Promise.reject(new KnowledgeHostError('request_too_large', 'The knowledge request exceeded 16KiB.'))
  }
  const pending = listenForKnowledgeResponse(
    operation,
    request.request_id,
    bindingFromPayload(payload),
    timeoutMs,
    signal,
  )
  hostWindow().chrome!.webview!.postMessage(serialized)
  return pending
}

export {
  MAX_KNOWLEDGE_REQUEST_BYTES,
  MAX_KNOWLEDGE_RESPONSE_BYTES,
  knowledgeHostRequestSchema,
  knowledgeHostResponseSchema,
} from './knowledgeTypes'
export { KnowledgeHostError } from './knowledgeErrors'
