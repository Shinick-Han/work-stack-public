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
