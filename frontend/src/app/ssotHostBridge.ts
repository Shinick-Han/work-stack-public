export type SsotConnectionState = 'ready' | 'testing' | 'saved' | 'reconnecting' | 'disconnected' | 'error'
export type SsotStorageMode = 'local' | 'ssh-remote'

export interface SsotRemoteProfile {
  ssh_host_alias: string
  remote_app_dir: string
  remote_data_dir: string
  local_forward_port: number
  remote_port: number
  workspace_id: string
}

export interface SsotConnectionStatus {
  type: 'workstack-ssot-connection-status'
  state: SsotConnectionState
  storage_mode: SsotStorageMode
  profile: Partial<SsotRemoteProfile>
  message: string
  restart_required: boolean
  log_path: string
  session_change_detection?: boolean
  runtime_forward_port?: number | null
  remote_product_version?: string
  remote_protocol_version?: number | null
}

export type SsotConnectionDraft =
  | { storage_mode: 'local' }
  | ({ storage_mode: 'ssh-remote' } & SsotRemoteProfile)

interface WebViewMessageEvent extends Event { data?: unknown }
interface SsotHostWindow extends Window {
  chrome?: {
    webview?: {
      addEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      removeEventListener?: (type: 'message', listener: (event: WebViewMessageEvent) => void) => void
      postMessage: (message: string) => void
    }
  }
}

const PREFIX = 'workstack-ssot-host'
const states = new Set<SsotConnectionState>(['ready', 'testing', 'saved', 'reconnecting', 'disconnected', 'error'])
const hostWindow = () => window as SsotHostWindow

export function hasSsotHost(): boolean {
  const bridge = hostWindow().chrome?.webview
  return typeof bridge?.postMessage === 'function' && typeof bridge.addEventListener === 'function'
}

function isRemoteProfile(value: unknown): value is SsotRemoteProfile {
  if (!value || typeof value !== 'object') return false
  const profile = value as Partial<SsotRemoteProfile>
  return typeof profile.ssh_host_alias === 'string'
    && typeof profile.remote_app_dir === 'string'
    && typeof profile.remote_data_dir === 'string'
    && Number.isInteger(profile.local_forward_port)
    && Number.isInteger(profile.remote_port)
    && typeof profile.workspace_id === 'string'
}

function isStatusProfile(value: unknown, mode: unknown): value is Partial<SsotRemoteProfile> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  if (mode === 'local') return Object.keys(value).length === 0
  return isRemoteProfile(value)
}

function isOptionalBoolean(value: unknown): value is boolean | undefined {
  return value === undefined || typeof value === 'boolean'
}

function isRuntimePort(value: unknown): value is number | null | undefined {
  if (value === undefined || value === null) return true
  return Number.isInteger(value) && Number(value) >= 1 && Number(value) <= 65535
}

function isOptionalRemoteVersion(value: unknown): value is string | undefined {
  return value === undefined || (typeof value === 'string' && value.length <= 64)
}

function isOptionalRemoteProtocol(value: unknown): value is number | null | undefined {
  return value === undefined || value === null || (Number.isInteger(value) && Number(value) >= 0)
}

function isStatus(value: unknown): value is SsotConnectionStatus {
  if (!value || typeof value !== 'object') return false
  const status = value as Partial<SsotConnectionStatus>
  return status.type === 'workstack-ssot-connection-status'
    && states.has(status.state as SsotConnectionState)
    && (status.storage_mode === 'local' || status.storage_mode === 'ssh-remote')
    && isStatusProfile(status.profile, status.storage_mode)
    && typeof status.message === 'string'
    && typeof status.restart_required === 'boolean'
    && typeof status.log_path === 'string'
    && isOptionalBoolean(status.session_change_detection)
    && isRuntimePort(status.runtime_forward_port)
    && isOptionalRemoteVersion(status.remote_product_version)
    && isOptionalRemoteProtocol(status.remote_protocol_version)
}

export function subscribeSsotConnectionStatus(listener: (status: SsotConnectionStatus) => void): () => void {
  const bridge = hostWindow().chrome?.webview
  if (!bridge?.addEventListener) return () => undefined
  const receive = (event: WebViewMessageEvent) => {
    if (isStatus(event.data)) listener(event.data)
  }
  bridge.addEventListener('message', receive)
  return () => bridge.removeEventListener?.('message', receive)
}

function send(command: string, draft?: SsotConnectionDraft) {
  const payload = draft ? `|${encodeURIComponent(JSON.stringify(draft))}` : ''
  hostWindow().chrome?.webview?.postMessage(`${PREFIX}|${command}${payload}`)
}

export const requestSsotConnectionStatus = () => send('status')
export const requestSsotConnectionTest = (draft: SsotConnectionDraft) => send('test', draft)
export const saveSsotConnection = (draft: SsotConnectionDraft) => send('save', draft)
export const requestSsotReconnect = () => send('reconnect')
export const requestSsotDiagnostics = () => send('open-diagnostics')
export function coordinateSsotWorkspaceRebind(workspaceId: string): Promise<void> {
  const bridge = hostWindow().chrome?.webview
  if (!bridge?.addEventListener) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const receive = (event: WebViewMessageEvent) => {
      const value = event.data as { type?: unknown; workspace_id?: unknown } | undefined
      if (value?.type !== 'workstack-ssot-rebind-ready' || value.workspace_id !== workspaceId) return
      window.clearTimeout(timeout)
      bridge.removeEventListener?.('message', receive)
      resolve()
    }
    const timeout = window.setTimeout(() => {
      bridge.removeEventListener?.('message', receive)
      reject(new Error('Native SSOT rebind coordination timed out before the remote commit'))
    }, 3000)
    bridge.addEventListener?.('message', receive)
    bridge.postMessage(`${PREFIX}|rebind-start|${workspaceId}`)
  })
}
export const notifySsotWorkspaceRebound = (workspaceId: string) => {
  hostWindow().chrome?.webview?.postMessage(`${PREFIX}|rebind-complete|${workspaceId}`)
}
