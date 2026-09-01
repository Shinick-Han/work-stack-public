import { z } from 'zod'

import {
  FEDERATION_SCHEMA_VERSION,
  MAX_FEDERATED_WORKSPACES,
  canonicalFederationUuidSchema,
  federatedEntityRefSchema,
  federationStatusMessageSchema,
  portfolioProjectionSchema,
  type FederatedEntityRef,
  type FederationStatusMessage,
  type PortfolioProjection,
} from '../domain/federationSchemas'

export const MAX_STATUS_MESSAGE_BYTES = 64 * 1024
export const MAX_PORTFOLIO_MESSAGE_BYTES = 1024 * 1024

interface WebViewMessageEvent extends Event { data?: unknown }
interface FederationHostWindow extends Window {
  chrome?: {
    webview?: {
      addEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      removeEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      postMessage: (message: string) => void
    }
  }
}

const federationHostRequestBaseSchema = z.discriminatedUnion('operation', [
  z.object({
    type: z.literal('workstack-federation-request'),
    schema_version: z.literal(FEDERATION_SCHEMA_VERSION),
    operation: z.literal('status'),
  }).strict(),
  z.object({
    type: z.literal('workstack-federation-request'),
    schema_version: z.literal(FEDERATION_SCHEMA_VERSION),
    operation: z.literal('read'),
    profile_ids: z.array(canonicalFederationUuidSchema).min(1).max(MAX_FEDERATED_WORKSPACES).readonly().optional(),
  }).strict(),
  z.object({
    type: z.literal('workstack-federation-request'),
    schema_version: z.literal(FEDERATION_SCHEMA_VERSION),
    operation: z.literal('switch'),
    workspace_id: canonicalFederationUuidSchema,
    target: federatedEntityRefSchema.optional(),
  }).strict(),
])

export const federationHostRequestSchema = federationHostRequestBaseSchema.superRefine((request, context) => {
  if (request.operation === 'switch' && request.target !== undefined
    && request.target.workspace_id !== request.workspace_id) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'Switch target must belong to the requested workspace authority',
      path: ['target', 'workspace_id'],
    })
  }
})

export type FederationHostRequest = z.infer<typeof federationHostRequestSchema>

const hostWindow = () => window as FederationHostWindow

export function hasFederationHost(): boolean {
  const bridge = hostWindow().chrome?.webview
  return typeof bridge?.postMessage === 'function' && typeof bridge.addEventListener === 'function'
}

function hasBoundedSerializedSize(value: unknown, limit: number): boolean {
  try {
    const encoded = JSON.stringify(value)
    return encoded !== undefined && new TextEncoder().encode(encoded).byteLength <= limit
  } catch {
    return false
  }
}

function subscribeBounded<T>(
  schema: z.ZodType<T>,
  byteLimit: number,
  listener: (message: T) => void,
): () => void {
  const bridge = hostWindow().chrome?.webview
  if (!bridge?.addEventListener) return () => undefined
  const receive = (event: WebViewMessageEvent) => {
    if (!hasBoundedSerializedSize(event.data, byteLimit)) return
    const parsed = schema.safeParse(event.data)
    if (parsed.success) listener(parsed.data)
  }
  bridge.addEventListener('message', receive)
  return () => bridge.removeEventListener?.('message', receive)
}

export function subscribeFederationStatus(listener: (status: FederationStatusMessage) => void): () => void {
  return subscribeBounded(federationStatusMessageSchema, MAX_STATUS_MESSAGE_BYTES, listener)
}

export function subscribePortfolioProjection(listener: (projection: PortfolioProjection) => void): () => void {
  return subscribeBounded(portfolioProjectionSchema, MAX_PORTFOLIO_MESSAGE_BYTES, listener)
}

function send(request: FederationHostRequest): boolean {
  const bridge = hostWindow().chrome?.webview
  if (typeof bridge?.postMessage !== 'function') return false
  const validated = federationHostRequestSchema.parse(request)
  bridge.postMessage(JSON.stringify(validated))
  return true
}

export function requestFederationStatus(): boolean {
  return send({
    type: 'workstack-federation-request',
    schema_version: FEDERATION_SCHEMA_VERSION,
    operation: 'status',
  })
}

export function requestPortfolioProjection(profileIds?: readonly string[]): boolean {
  return send({
    type: 'workstack-federation-request',
    schema_version: FEDERATION_SCHEMA_VERSION,
    operation: 'read',
    ...(profileIds === undefined ? {} : { profile_ids: profileIds }),
  })
}

export function requestFederationWorkspaceSwitch(workspaceId: string, target?: FederatedEntityRef): boolean {
  return send({
    type: 'workstack-federation-request',
    schema_version: FEDERATION_SCHEMA_VERSION,
    operation: 'switch',
    workspace_id: workspaceId,
    ...(target === undefined ? {} : { target }),
  })
}
