import { useEffect, useRef, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'

import { Button, Pill } from '../components/Primitives'
import { workstackConnectionRecoveryEnabled } from '../config/connectionRecoveryGate'
import {
  connectionActivationRecoveryStateSchema,
  createConnectionActivationRecoveryRequest,
  type ConnectionActivationRecoveryOperation,
  type ConnectionActivationRecoveryRequest,
  type ConnectionActivationRecoveryState,
} from './connectionActivationRecoveryContract'

interface ConnectionActivationRecoveryProps {
  actionError?: string | null
  enabled?: boolean
  onExit: () => void
  onRestore: () => void
  pendingOperation?: ConnectionActivationRecoveryOperation | null
  state: ConnectionActivationRecoveryState
}

export interface ConnectionActivationRecoveryMountOptions {
  enabled?: boolean
  onAction: (request: ConnectionActivationRecoveryRequest) => void | Promise<void>
  state: unknown
}

function shortIdentity(value: string): string {
  return `${value.slice(0, 8)}…${value.slice(-4)}`
}

function ProfileSummary({ label, profile }: {
  label: string
  profile: ConnectionActivationRecoveryState['failed_profile']
}) {
  return <div className="activation-recovery__profile">
    <dt>{label}</dt>
    <dd><strong>{profile.label}</strong><Pill>{profile.kind === 'local' ? 'Local' : 'Remote SSH'}</Pill></dd>
    <dd><span>Profile identity</span><code>{shortIdentity(profile.profile_id)}</code></dd>
  </div>
}

export function ConnectionActivationRecovery({
  actionError = null,
  enabled = false,
  onExit,
  onRestore,
  pendingOperation = null,
  state,
}: ConnectionActivationRecoveryProps) {
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => { if (enabled) headingRef.current?.focus() }, [enabled])
  if (!enabled) return null
  const restoring = pendingOperation === 'restore-previous-connection'
  const exiting = pendingOperation === 'exit'
  return <main className="activation-recovery">
    <section aria-describedby="activation-recovery-description" aria-labelledby="activation-recovery-title" className="activation-recovery__surface" role="alertdialog">
      <header>
        <Pill tone="danger">Startup recovery</Pill>
        <h1 id="activation-recovery-title" ref={headingRef} tabIndex={-1}>Work Stack could not activate this connection</h1>
        <p id="activation-recovery-description">No SSOT content was copied, merged, or deleted. Choose the next startup action explicitly.</p>
      </header>
      <dl aria-label="Connection recovery summary">
        <ProfileSummary label="Failed profile" profile={state.failed_profile} />
        {state.previous_profile ? <ProfileSummary label="Previous connection" profile={state.previous_profile} /> : <div className="activation-recovery__profile"><dt>Previous connection</dt><dd>None is available to restore.</dd></div>}
      </dl>
      <div className="activation-recovery__error" role="alert"><strong>{state.error.summary}</strong><span>Error code: {state.error.code}</span></div>
      {pendingOperation ? <p aria-live="polite" role="status">{restoring ? 'Requesting a restore of the previous connection…' : 'Closing Work Stack…'}</p> : null}
      {actionError ? <p className="inline-error" role="alert">{actionError}</p> : null}
      <footer>
        <Button disabled={!state.previous_profile || pendingOperation !== null} onClick={onRestore} variant="primary">{restoring ? 'Restoring…' : 'Restore previous connection'}</Button>
        <Button disabled={pendingOperation !== null} onClick={onExit} variant="ghost">{exiting ? 'Exiting…' : 'Exit'}</Button>
      </footer>
    </section>
  </main>
}

function RecoveryEntry({ onAction, state }: {
  onAction: ConnectionActivationRecoveryMountOptions['onAction']
  state: ConnectionActivationRecoveryState
}) {
  const actionGate = useRef(false)
  const [pendingOperation, setPendingOperation] = useState<ConnectionActivationRecoveryOperation | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const run = (operation: ConnectionActivationRecoveryOperation) => {
    if (actionGate.current) return
    actionGate.current = true
    setPendingOperation(operation)
    setActionError(null)
    const request = createConnectionActivationRecoveryRequest(state, operation)
    void Promise.resolve(onAction(request)).catch(() => {
      setActionError('The recovery request was not accepted. You can retry or exit Work Stack.')
    }).finally(() => {
      actionGate.current = false
      setPendingOperation(null)
    })
  }
  return <ConnectionActivationRecovery actionError={actionError} enabled onExit={() => run('exit')}
    onRestore={() => run('restore-previous-connection')} pendingOperation={pendingOperation} state={state} />
}

/** Standalone entry for a native recovery page. It performs no host action until a visible button is activated. */
export function mountConnectionActivationRecovery(
  container: Element,
  options: ConnectionActivationRecoveryMountOptions,
): Root {
  const root = createRoot(container)
  const enabled = options.enabled ?? workstackConnectionRecoveryEnabled
  if (!enabled) {
    root.render(null)
    return root
  }
  const state = connectionActivationRecoveryStateSchema.parse(options.state)
  root.render(<RecoveryEntry onAction={options.onAction} state={state} />)
  return root
}
