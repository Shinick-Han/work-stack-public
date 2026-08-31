export type UpdateState = 'idle' | 'checking' | 'current' | 'available' | 'downloading' | 'ready' | 'installing' | 'blocked' | 'error'

export interface UpdatePreferences {
  auto_check: boolean
  auto_download: boolean
  install_on_exit: boolean
}

export interface UpdateHostStatus {
  type: 'workstack-update-status'
  state: UpdateState
  current_version: string
  latest_version: string
  release_url: string
  message: string
  preferences: UpdatePreferences
}

interface WebViewMessageEvent extends Event { data?: unknown }
interface UpdateHostWindow extends Window {
  chrome?: {
    webview?: {
      addEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      removeEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      postMessage: (message: string) => void
    }
  }
}

const PREFIX = 'workstack-update-host'
const states = new Set<UpdateState>(['idle', 'checking', 'current', 'available', 'downloading', 'ready', 'installing', 'blocked', 'error'])
const hostWindow = () => window as UpdateHostWindow

export function hasUpdateHost(): boolean {
  const bridge = hostWindow().chrome?.webview
  return typeof bridge?.postMessage === 'function' && typeof bridge.addEventListener === 'function'
}

function isStatus(value: unknown): value is UpdateHostStatus {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<UpdateHostStatus>
  const preferences = item.preferences as Partial<UpdatePreferences> | undefined
  return item.type === 'workstack-update-status'
    && states.has(item.state as UpdateState)
    && typeof item.current_version === 'string'
    && typeof item.latest_version === 'string'
    && typeof item.release_url === 'string'
    && typeof item.message === 'string'
    && typeof preferences?.auto_check === 'boolean'
    && typeof preferences.auto_download === 'boolean'
    && typeof preferences.install_on_exit === 'boolean'
}

export function subscribeUpdateStatus(listener: (status: UpdateHostStatus) => void): () => void {
  const bridge = hostWindow().chrome?.webview
  if (!bridge?.addEventListener) return () => undefined
  const receive = (event: WebViewMessageEvent) => {
    if (isStatus(event.data)) listener(event.data)
  }
  bridge.addEventListener('message', receive)
  return () => bridge.removeEventListener?.('message', receive)
}

function send(command: string) {
  hostWindow().chrome?.webview?.postMessage(`${PREFIX}|${command}`)
}

export const requestUpdateStatus = () => send('status')
export const requestUpdateCheck = () => send('check')
export const requestUpdateDownload = () => send('download')
export const requestUpdateInstall = () => send('install')
export const openUpdateRelease = () => send('open-release')
export function saveUpdatePreferences(preferences: UpdatePreferences) {
  send(`preferences|${preferences.auto_check ? 1 : 0}|${preferences.auto_download ? 1 : 0}|${preferences.install_on_exit ? 1 : 0}`)
}
