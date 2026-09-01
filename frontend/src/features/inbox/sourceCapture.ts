import { capturePacketSchema } from '../../domain/schemas'
import type { CapturePacket, TaskPriority } from '../../domain/types'
import { isSourceProviderKey, type SourceProviderKey } from './sourceProviders'

export const EXTERNAL_CAPTURE_MESSAGE = 'workstack.source-capture.v1'
export const EXTERNAL_CAPTURE_ACK = 'workstack.source-capture.ack.v1'
export const MAX_EXTERNAL_CAPTURE_TEXT = 4000

export interface SourceCaptureDraft {
  intentId: string
  provider: SourceProviderKey
  captureTitle: string
  text: string
  taskTitle: string | null
  taskDetail: string | null
  sourceUrl: string
  capturedAt: string
  priority: TaskPriority
  due: string | null
  objectiveIds: string[]
  taskId: string | null
}

export type MicrosoftSourceLinkKind = 'item' | 'app' | 'auth' | 'missing'
export interface ExternalSourceCapture {
  provider: SourceProviderKey
  title: string
  text: string
  sourceUrl: string
  capturedAt: string
}

const allowedMicrosoftHosts = [
  '.office.com',
  '.office365.com',
  '.microsoft.com',
  '.microsoftonline.com',
  '.sharepoint.com',
  '.cloud.microsoft',
]

const allowedExactMicrosoftHosts = new Set([
  'login.live.com',
  'onedrive.live.com',
  'outlook.live.com',
  'teams.live.com',
  'www.onenote.com',
])

const sensitiveUrlParameters = new Set([
  'accesstoken', 'refreshtoken', 'idtoken', 'oauthtoken', 'oauthcode',
  'authorization', 'authorizationcode', 'bearer', 'token', 'clientsecret',
  'password', 'passwd', 'apikey', 'secret', 'code', 'to', 'cc', 'bcc',
  'recipient', 'recipients',
])

function hasAllowedMicrosoftHost(hostname: string) {
  const normalized = hostname.toLowerCase()
  return allowedExactMicrosoftHosts.has(normalized) || allowedMicrosoftHosts.some((suffix) => normalized.endsWith(suffix))
}

function isSensitiveUrlParameter(value: string) {
  return sensitiveUrlParameters.has(value.toLowerCase().replace(/[^a-z0-9]/g, ''))
}

function fragmentContainsSensitiveParameter(fragment: string) {
  try {
    return decodeURIComponent(fragment).split(/[?&#]/).some((part) => {
      const separator = part.indexOf('=')
      return separator >= 0 && isSensitiveUrlParameter(part.slice(0, separator))
    })
  } catch {
    return true
  }
}

function providerForMicrosoftHost(hostname: string): SourceProviderKey | null {
  const normalized = hostname.toLowerCase()
  if (normalized === 'outlook.live.com' || normalized.startsWith('outlook.')) return 'outlook'
  if (normalized === 'teams.live.com' || normalized.startsWith('teams.')) return 'teams'
  if (normalized === 'www.onenote.com') return 'onenote'
  return null
}

export function createSourceCaptureIntentId() {
  const webCrypto = globalThis.crypto as Crypto | undefined
  if (webCrypto?.randomUUID) return webCrypto.randomUUID()
  const bytes = new Uint8Array(16)
  if (webCrypto) webCrypto.getRandomValues(bytes)
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function sanitizeMicrosoftSourceUrlForProvider(provider: SourceProviderKey, value: string) {
  const safeUrl = sanitizeMicrosoftSourceUrl(value)
  if (!safeUrl) return null
  const detectedProvider = providerForMicrosoftHost(new URL(safeUrl).hostname)
  return detectedProvider && detectedProvider !== provider ? null : safeUrl
}

export function sanitizeMicrosoftSourceUrl(value: string): string | null {
  try {
    const parsed = new URL(value)
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return null
    if (parsed.port && parsed.port !== '443') return null
    if (!hasAllowedMicrosoftHost(parsed.hostname)) return null
    if ([...parsed.searchParams.keys()].some(isSensitiveUrlParameter)) return null
    if (fragmentContainsSensitiveParameter(parsed.hash.slice(1))) return null
    return parsed.href
  } catch {
    return null
  }
}

export function classifyMicrosoftSourceUrl(provider: SourceProviderKey, value: string): MicrosoftSourceLinkKind {
  const safeUrl = sanitizeMicrosoftSourceUrlForProvider(provider, value)
  if (!safeUrl) return 'missing'
  const parsed = new URL(safeUrl)
  const host = parsed.hostname.toLowerCase()
  const path = parsed.pathname.toLowerCase()
  if (host === 'login.live.com' || host.endsWith('.microsoftonline.com')) return 'auth'
  if (provider === 'outlook' && (/\/id\//.test(path) || path.includes('/deeplink/read/'))) return 'item'
  if (provider === 'teams' && (path.includes('/l/message/') || parsed.searchParams.has('messageId'))) return 'item'
  if (provider === 'onenote' && (parsed.searchParams.has('id') || parsed.searchParams.has('resid'))) return 'item'
  return 'app'
}

export function parseExternalSourceCapture(value: unknown): ExternalSourceCapture | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Record<string, unknown>
  if (!isSourceProviderKey(candidate.provider)) return null
  if (typeof candidate.title !== 'string' || typeof candidate.text !== 'string' || typeof candidate.sourceUrl !== 'string' || typeof candidate.capturedAt !== 'string') return null
  const title = candidate.title.trim().slice(0, 500)
  const text = candidate.text.trim().slice(0, MAX_EXTERNAL_CAPTURE_TEXT)
  if (!title || !text || !Number.isFinite(Date.parse(candidate.capturedAt))) return null
  return {
    provider: candidate.provider,
    title,
    text,
    sourceUrl: candidate.sourceUrl.slice(0, 4096),
    capturedAt: new Date(candidate.capturedAt).toISOString(),
  }
}

async function sha256Text(value: string) {
  const bytes = new TextEncoder().encode(value)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return `sha256:${[...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')}`
}

export async function buildManualWebCapturePacket(draft: SourceCaptureDraft): Promise<CapturePacket> {
  const capturedAt = new Date(draft.capturedAt).toISOString()
  const safeUrl = sanitizeMicrosoftSourceUrlForProvider(draft.provider, draft.sourceUrl)
  const locatorSeed = `${draft.provider}\n${safeUrl ?? ''}\n${capturedAt}\n${draft.captureTitle}`
  const locatorDigest = (await sha256Text(locatorSeed)).slice('sha256:'.length)
  const source = {
    provider: 'manual',
    resource_type: `microsoft-web.${draft.provider}`,
    connection_ref: 'browser-user-selection',
    container_ref: `microsoft-${draft.provider}`,
    object_ref: `web-capture:${locatorDigest}`,
    version_ref: `captured:${capturedAt}`,
    display_title: draft.captureTitle.trim().slice(0, 500),
    web_url: safeUrl,
    retrieved_at: capturedAt,
    fingerprint: '',
  }
  source.fingerprint = await sha256Text([
    source.provider,
    source.connection_ref,
    source.container_ref,
    source.object_ref,
    source.version_ref,
  ].join('\n'))
  const packet: CapturePacket = {
    schema_version: '1.0',
    source_key: await sha256Text([
      source.provider,
      source.connection_ref,
      source.container_ref,
      source.object_ref,
    ].join('\n')),
    source,
    normalized: {
      summary: draft.text.trim().slice(0, 2000),
      context: draft.text.trim().slice(0, 4000),
      action_items: draft.taskTitle ? [{
        title: draft.taskTitle.trim(),
        detail: draft.taskDetail?.trim() ?? '',
        priority: draft.priority,
        due: draft.due,
      }] : [],
      tags: [`source:${draft.provider}`, 'manual-web-capture'],
    },
    task_hints: [],
    provenance: {
      capture_mode: 'manual',
      adapter: 'microsoft-web-capture',
      adapter_version: '1.0.0',
      redaction_policy_version: 'workstack-redaction-v1',
      raw_retained: false,
      created_at: capturedAt,
    },
  }
  return capturePacketSchema.parse(packet)
}
