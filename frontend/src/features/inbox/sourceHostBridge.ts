import type { SourceProviderKey } from './sourceProviders'

const SOURCE_HOST_PREFIX = 'workstack-source-host'

interface WebViewHostWindow extends Window {
  chrome?: {
    webview?: {
      postMessage: (message: string) => void
      addEventListener?: (type: 'message', listener: (event: MessageEvent) => void) => void
      removeEventListener?: (type: 'message', listener: (event: MessageEvent) => void) => void
    }
  }
}

let sourceCaptureRequestSequence = 0

export interface EmbeddedSourceDraft {
  url: string
  title: string
  text: string
}

export type EmbeddedSourceZoom = Record<SourceProviderKey, number>

const defaultSourceZoom: EmbeddedSourceZoom = { outlook: 100, teams: 100, onenote: 100 }

function parseSourceZoom(payload: unknown): EmbeddedSourceZoom | null {
  const message = payload as { type?: unknown; values?: unknown } | null
  if (!message || message.type !== 'workstack-source-zoom' || !message.values || typeof message.values !== 'object') return null
  const values = message.values as Record<string, unknown>
  const parsed = { ...defaultSourceZoom }
  for (const provider of Object.keys(parsed) as SourceProviderKey[]) {
    const value = values[provider]
    if (!Number.isInteger(value) || (value as number) < 50 || (value as number) > 200) return null
    parsed[provider] = value as number
  }
  return parsed
}

function hostWindow(): WebViewHostWindow {
  return window as WebViewHostWindow
}

function roundedPosition(value: number) {
  return Math.round(value)
}

function roundedSize(value: number) {
  return Math.max(0, Math.round(value))
}

function nativePixelScale() {
  const scale = window.devicePixelRatio
  return Number.isFinite(scale) && scale >= 0.5 && scale <= 4 ? scale : 1
}

export function embeddedSourceHostAvailable() {
  return typeof hostWindow().chrome?.webview?.postMessage === 'function'
}

export function showEmbeddedSource(provider: SourceProviderKey, rect: Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>) {
  if (!embeddedSourceHostAvailable()) return false
  const scale = nativePixelScale()
  hostWindow().chrome!.webview!.postMessage([
    SOURCE_HOST_PREFIX,
    'show',
    provider,
    roundedPosition(rect.left * scale),
    roundedPosition(rect.top * scale),
    roundedSize(rect.width * scale),
    roundedSize(rect.height * scale),
  ].join('|'))
  return true
}

export function hideEmbeddedSource() {
  if (!embeddedSourceHostAvailable()) return false
  hostWindow().chrome!.webview!.postMessage(`${SOURCE_HOST_PREFIX}|hide`)
  return true
}

export function suspendEmbeddedSource() {
  if (!embeddedSourceHostAvailable()) return false
  hostWindow().chrome!.webview!.postMessage(`${SOURCE_HOST_PREFIX}|suspend`)
  return true
}

export function resumeEmbeddedSource() {
  if (!embeddedSourceHostAvailable()) return false
  hostWindow().chrome!.webview!.postMessage(`${SOURCE_HOST_PREFIX}|resume`)
  return true
}

export function requestEmbeddedSourceZoom() {
  if (!embeddedSourceHostAvailable()) return false
  hostWindow().chrome!.webview!.postMessage(`${SOURCE_HOST_PREFIX}|zoom-status`)
  return true
}

export function setEmbeddedSourceZoom(provider: SourceProviderKey, value: number) {
  if (!embeddedSourceHostAvailable() || !Number.isInteger(value) || value < 50 || value > 200) return false
  hostWindow().chrome!.webview!.postMessage(`${SOURCE_HOST_PREFIX}|zoom|${provider}|${value}`)
  return true
}

export function subscribeEmbeddedSourceZoom(listener: (values: EmbeddedSourceZoom) => void) {
  const bridge = hostWindow().chrome?.webview
  if (!bridge?.addEventListener || !bridge.removeEventListener) return () => undefined
  const receive = (event: MessageEvent) => {
    const values = parseSourceZoom(event.data)
    if (values) listener(values)
  }
  bridge.addEventListener('message', receive)
  return () => bridge.removeEventListener?.('message', receive)
}

export function requestEmbeddedSourceDraft(provider: SourceProviderKey, timeoutMs = 2000): Promise<EmbeddedSourceDraft | null> {
  const bridge = hostWindow().chrome?.webview
  if (!bridge || typeof bridge.addEventListener !== 'function' || typeof bridge.removeEventListener !== 'function') {
    return Promise.resolve(null)
  }
  const addEventListener = bridge.addEventListener.bind(bridge)
  const removeEventListener = bridge.removeEventListener.bind(bridge)
  const requestId = `source-capture-${Date.now().toString(36)}-${++sourceCaptureRequestSequence}`
  return new Promise((resolve) => {
    let timer = 0
    const finish = (value: EmbeddedSourceDraft | null) => {
      window.clearTimeout(timer)
      removeEventListener('message', receive)
      resolve(value)
    }
    const receive = (event: MessageEvent) => {
      const payload = event.data as { type?: unknown; request_id?: unknown; provider?: unknown; url?: unknown; title?: unknown; text?: unknown } | null
      if (!payload || payload.type !== 'workstack-source-draft' || payload.request_id !== requestId || payload.provider !== provider) return
      if (typeof payload.url !== 'string' || payload.url.length > 4096 || typeof payload.title !== 'string' || payload.title.length > 500 || typeof payload.text !== 'string' || payload.text.length > 4000) {
        finish(null)
        return
      }
      finish({ url: payload.url, title: payload.title, text: payload.text })
    }
    addEventListener('message', receive)
    timer = window.setTimeout(() => finish(null), Math.max(100, timeoutMs))
    bridge.postMessage(`${SOURCE_HOST_PREFIX}|capture|${provider}|${requestId}`)
  })
}
