import { z } from 'zod'

export const sha256 = z.string().regex(/^sha256:[0-9a-f]{64}$/, 'Expected sha256: followed by 64 lowercase hex characters')
export const safeExternalUrl = z.string().url().refine(
  (value) => new URL(value).protocol === 'https:',
  'Source links must use HTTPS.',
)

const urlCredentialNames = new Set([
  'accesstoken',
  'refreshtoken',
  'idtoken',
  'oauthtoken',
  'oauthcode',
  'authorization',
  'authorizationcode',
  'bearer',
  'token',
  'clientsecret',
  'password',
  'passwd',
  'apikey',
  'secret',
  'code',
])
export const urlCredentialAssignment = /(?<![A-Za-z0-9])(?:(?:access|refresh|id|oauth)[_.-]?token|(?:oauth|authorization)[_.-]?code|authorization|bearer|token|client[_.-]?secret|password|passwd|api[_.-]?key|secret|code)(?![A-Za-z0-9])["']?\s*[:=]/i
export const credentialValue = /(?:\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}|(?<![A-Za-z0-9])(?:(?:access|refresh|id|oauth)[_.-]?token|client[_.-]?(?:secret|assertion)|authorization|bearer|token|password|passwd|api[_.-]?key|secret|saml[_.-]?response)(?![A-Za-z0-9])["']?\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{12,}|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|-----BEGIN\s+[A-Z ]*PRIVATE\s+KEY-----)/i
export const unsafeRawCanary = /(?:RAW|ATTACHMENT)_CANARY_DO_NOT_STORE/i
export const htmlTag = /<\/?[A-Za-z][^>]*>/
export const controlOrFormat = /[\p{Cc}\p{Cf}]/u
export const emailAddress = /[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/
export const mailHeaderPrefix = /^(?:from|to|cc|bcc|subject|sent|date):/im
export const recipientAssignment = /(?<![A-Za-z0-9])["']?(?:to|cc|bcc|recipients?)["']?\s*[:=]/i
const urlRecipientNames = new Set(['to', 'cc', 'bcc', 'recipient', 'recipients'])
const validationPercentDecodeRounds = 5

function decodePercentOnce(value: string) {
  return value.replace(/(?:%[0-9a-f]{2})+/gi, (encoded) => {
    const bytes = encoded.split('%').slice(1).map((pair) => Number.parseInt(pair, 16))
    return new TextDecoder().decode(new Uint8Array(bytes))
  })
}

export function decodeForValidation(value: string): string | null {
  let decoded = value
  for (let attempt = 0; attempt < validationPercentDecodeRounds; attempt += 1) {
    const candidate = decodePercentOnce(decoded)
    if (candidate === decoded) return decoded
    decoded = candidate
  }
  return decodePercentOnce(decoded) === decoded ? decoded : null
}

function decodedUrlContainsCredentialMaterial(decoded: string) {
  if (urlCredentialAssignment.test(decoded) || credentialValue.test(decoded)) return true
  try {
    const parsed = new URL(decoded)
    for (const component of [parsed.search.slice(1), parsed.hash.slice(1)]) {
      const params = new URLSearchParams(component)
      for (const key of params.keys()) {
        const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, '')
        if (urlCredentialNames.has(normalized)) return true
      }
    }
  } catch {
    return false
  }
  return false
}

function decodedUrlContainsRecipientMaterial(decoded: string) {
  if (recipientAssignment.test(decoded)) return true
  try {
    const parsed = new URL(decoded)
    for (const component of [parsed.search.slice(1), parsed.hash.slice(1)]) {
      const params = new URLSearchParams(component)
      for (const key of params.keys()) {
        const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, '')
        if (urlRecipientNames.has(normalized)) return true
      }
    }
  } catch {
    return false
  }
  return false
}

export function isSafeMicrosoftUrl(value: string) {
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    return false
  }
  const decoded = decodeForValidation(value)
  if (decoded === null) return false
  const hostname = parsed.hostname.toLowerCase()
  const allowedHost = [
    '.microsoft.com',
    '.office.com',
    '.office365.com',
    '.microsoftonline.com',
    '.sharepoint.com',
    '.cloud.microsoft',
  ].some((suffix) => hostname.endsWith(suffix))
  return parsed.protocol === 'https:'
    && !parsed.username
    && !parsed.password
    && (!parsed.port || parsed.port === '443')
    && allowedHost
    && !controlOrFormat.test(decoded)
    && !htmlTag.test(decoded)
    && !unsafeRawCanary.test(decoded)
    && !emailAddress.test(decoded)
    && !decodedUrlContainsCredentialMaterial(decoded)
    && !decodedUrlContainsRecipientMaterial(decoded)
}

export const safeMicrosoftUrl = z.string().max(4096).url().refine(
  isSafeMicrosoftUrl,
  'Receipt links must use a token-free, allowlisted Microsoft HTTPS host.',
)
export const isoDateTime = z.string().datetime({ offset: true })
export const boundedReference = z.string().min(1).max(512)
export const safeSourceLocator = z.string().min(1).max(1024).refine((value) => {
  const decoded = decodeForValidation(value)
  return decoded !== null && !recipientAssignment.test(decoded)
}, 'Source locator must not contain recipient assignment material.')
