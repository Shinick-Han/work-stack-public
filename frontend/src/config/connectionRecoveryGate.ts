interface RecoveryGateEnvironment { VITE_WORKSTACK_CONNECTION_RECOVERY?: unknown }
interface RecoveryGateWindow extends Window { __WORKSTACK_FEATURES__?: { connectionActivationRecovery?: unknown } }

function explicitlyEnabled(value: unknown): boolean {
  return value === true || (typeof value === 'string' && value.trim().toLowerCase() === 'true')
}

export function connectionRecoveryGateFrom(environment: RecoveryGateEnvironment, runtimeValue: unknown): boolean {
  return explicitlyEnabled(environment.VITE_WORKSTACK_CONNECTION_RECOVERY) || explicitlyEnabled(runtimeValue)
}

const runtimeValue = typeof window === 'undefined'
  ? undefined
  : (window as RecoveryGateWindow).__WORKSTACK_FEATURES__?.connectionActivationRecovery

/** Startup recovery stays dark unless the build or trusted desktop bootstrap opts in explicitly. */
export const workstackConnectionRecoveryEnabled = connectionRecoveryGateFrom(import.meta.env, runtimeValue)
