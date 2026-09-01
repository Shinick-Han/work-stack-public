export interface ConnectionCenterGates {
  registry: boolean
  activation: boolean
}

interface ConnectionCenterEnvironment {
  VITE_WORKSTACK_CONNECTION_REGISTRY?: unknown
  VITE_WORKSTACK_CONNECTION_REGISTRY_ACTIVATION?: unknown
}

export interface ConnectionCenterRuntimeFlags {
  connectionRegistry?: unknown
  connectionRegistryActivation?: unknown
}

function explicitlyEnabled(value: unknown): boolean {
  return value === true || (typeof value === 'string' && value.trim().toLowerCase() === 'true')
}

export function connectionCenterGatesFrom(
  environment: ConnectionCenterEnvironment,
  runtime: ConnectionCenterRuntimeFlags | undefined,
  desktopHostAvailable = false,
): ConnectionCenterGates {
  const registry = desktopHostAvailable
    || explicitlyEnabled(environment.VITE_WORKSTACK_CONNECTION_REGISTRY)
    || explicitlyEnabled(runtime?.connectionRegistry)
  const activationRequested = desktopHostAvailable
    || explicitlyEnabled(environment.VITE_WORKSTACK_CONNECTION_REGISTRY_ACTIVATION)
    || explicitlyEnabled(runtime?.connectionRegistryActivation)
  return { registry, activation: registry && activationRequested }
}

interface FeatureWindow extends Window {
  __WORKSTACK_FEATURES__?: ConnectionCenterRuntimeFlags
}

const runtimeFlags = typeof window === 'undefined'
  ? undefined
  : (window as FeatureWindow).__WORKSTACK_FEATURES__

const desktopHostAvailable = typeof window !== 'undefined'
  && typeof (window as FeatureWindow & { chrome?: { webview?: { postMessage?: unknown; addEventListener?: unknown } } }).chrome?.webview?.postMessage === 'function'
  && typeof (window as FeatureWindow & { chrome?: { webview?: { postMessage?: unknown; addEventListener?: unknown } } }).chrome?.webview?.addEventListener === 'function'

/** The trusted desktop host exposes path selection by default; ordinary browsers remain dark. */
export const workstackConnectionCenterGates = connectionCenterGatesFrom(import.meta.env, runtimeFlags, desktopHostAvailable)
