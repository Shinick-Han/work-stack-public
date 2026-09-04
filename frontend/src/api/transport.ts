import { z } from 'zod'
import { publishPlanningChange } from '../integration/planningChangeBus'

const sessionSchema = z.object({ csrf_token: z.string().min(8) })
const envelopeSchema = z.object({ data: z.unknown(), meta: z.record(z.string(), z.unknown()).optional() })

interface ErrorEnvelope {
  error?: { code?: string; message?: string; details?: unknown }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

export class CommitUnknownError extends Error {
  readonly cause: unknown

  constructor(message: string, cause: unknown) {
    super(message)
    this.name = 'CommitUnknownError'
    this.cause = cause
  }
}

let csrfTokenPromise: Promise<string> | null = null

export async function fetchWithNetworkRetry(
  input: RequestInfo | URL,
  init: RequestInit,
): Promise<Response> {
  try {
    return await fetch(input, init)
  } catch (firstError) {
    if (init.signal?.aborted) throw firstError
    // The exact same init object is retried. For idempotent mutations this preserves the
    // Idempotency-Key generated for the logical operation.
    return fetch(input, init)
  }
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    throw new ApiError(response.status, 'invalid_json', 'The server returned an unreadable response.')
  }
}

export async function assertOk(response: Response): Promise<unknown> {
  const payload = await readJson(response)
  if (response.ok) return payload
  const candidate = payload as ErrorEnvelope | null
  throw new ApiError(
    response.status,
    candidate?.error?.code ?? 'request_failed',
    candidate?.error?.message ?? `Request failed with status ${response.status}.`,
    candidate?.error?.details,
  )
}

export async function getCsrfToken(force = false): Promise<string> {
  if (force) csrfTokenPromise = null
  csrfTokenPromise ??= (async () => {
    const response = await fetchWithNetworkRetry('/api/v1/session', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    })
    const payload = envelopeSchema.parse(await assertOk(response))
    return sessionSchema.parse(payload.data).csrf_token
  })()
  return csrfTokenPromise
}

export async function getData<T>(path: string, schema: z.ZodType<T>): Promise<T> {
  const response = await fetchWithNetworkRetry(path, {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Accept: 'application/json' },
  })
  const envelope = envelopeSchema.parse(await assertOk(response))
  return schema.parse(envelope.data)
}

/**
 * Opt-in behaviour for callers that must send exactly one mutation request.
 * Omitting it preserves the existing network retry and 403 recovery for every
 * current caller.
 */
/** Narrow response evidence the D5 route needs before the envelope is discarded. */
export interface MutateReceipt {
  status?: number
  meta?: unknown
}

export interface MutateOptions {
  /** D5 transitions: one POST per explicit submit, no automatic resend. */
  singleAttempt?: boolean
  /** Filled with the HTTP status and envelope meta of the answered request. */
  receipt?: MutateReceipt
}

/** An unreadable 2xx body: the write may well have happened. */
export class UnreadableSuccessError extends Error {
  constructor(readonly status: number) {
    super('The server returned an unreadable successful response.')
    this.name = 'UnreadableSuccessError'
  }
}

export async function mutateData<T>(
  path: string,
  method: 'POST' | 'PATCH',
  body: unknown,
  schema: z.ZodType<T>,
  idempotencyKey?: string,
  publishChange = true,
  options: MutateOptions = {},
): Promise<T> {
  const encodedBody = JSON.stringify(body)
  const makeHeaders = async (refreshSession = false) => {
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-WorkStack-CSRF': await getCsrfToken(refreshSession),
    }
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
    return headers
  }

  // Default behaviour is unchanged. Only an explicit single-attempt caller
  // gives up the network retry, and it also declines the 403 resend below.
  const request = options.singleAttempt
    ? fetch
    : idempotencyKey ? fetchWithNetworkRetry : fetch
  let response = await request(path, {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    headers: await makeHeaders(),
    body: encodedBody,
  })

  // A restarted local server rotates the CSRF nonce. Retry once with the same body and,
  // critically, the same idempotency key.
  if (response.status === 403 && !options.singleAttempt) {
    response = await request(path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: await makeHeaders(true),
      body: encodedBody,
    })
  }

  if (options.receipt) options.receipt.status = response.status
  let payload: unknown
  try {
    payload = await assertOk(response)
  } catch (error) {
    // A successful status with an unreadable body is ambiguous, not determinate.
    if (options.receipt && response.ok && error instanceof ApiError && error.code === 'invalid_json') {
      throw new UnreadableSuccessError(response.status)
    }
    throw error
  }
  const envelope = envelopeSchema.parse(payload)
  if (options.receipt) options.receipt.meta = envelope.meta
  const parsed = schema.parse(envelope.data)
  if (publishChange) publishPlanningChange()
  return parsed
}

export function createIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return `workstack:${crypto.randomUUID()}`
  }
  return `workstack:${Date.now()}:${Math.random().toString(36).slice(2, 14)}`
}

export async function mutateIdempotent<T>(
  path: string,
  body: unknown,
  schema: z.ZodType<T>,
  idempotencyKey: string,
  commitUnknownMessage: string,
  options: MutateOptions = {},
): Promise<T> {
  try {
    return await mutateData(path, 'POST', body, schema, idempotencyKey, true, options)
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new CommitUnknownError(commitUnknownMessage, error)
  }
}
