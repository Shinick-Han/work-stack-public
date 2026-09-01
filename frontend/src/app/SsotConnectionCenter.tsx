import { useEffect, useMemo, useState } from 'react'
import { Dialog } from '../components/Dialog'
import { Button } from '../components/Primitives'
import {
  hasSsotHost,
  requestSsotDiagnostics,
  requestSsotReconnect,
  requestSsotConnectionStatus,
  requestSsotConnectionTest,
  saveSsotConnection,
  subscribeSsotConnectionStatus,
  type SsotConnectionDraft,
  type SsotConnectionStatus,
  type SsotRemoteProfile,
  type SsotStorageMode,
} from './ssotHostBridge'

const emptyProfile: SsotRemoteProfile = {
  ssh_host_alias: '',
  remote_app_dir: '',
  remote_data_dir: '',
  local_forward_port: 18765,
  remote_port: 8765,
  workspace_id: '',
}

const safeAlias = /^[A-Za-z0-9_.@-]+$/
const safeRemotePath = /^\/(?!.*[\r\n\0]).+/
const canonicalUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

export function validateSsotDraft(mode: SsotStorageMode, profile: SsotRemoteProfile): string[] {
  if (mode === 'local') return []
  const errors: string[] = []
  if (!safeAlias.test(profile.ssh_host_alias)) errors.push('SSH host alias must use only letters, numbers, dots, dashes, underscores, or @. SSH arguments are not accepted.')
  if (!safeRemotePath.test(profile.remote_app_dir) || invalidRemotePath(profile.remote_app_dir)) errors.push('Remote app directory must be a non-root absolute Linux path without dot segments.')
  if (!safeRemotePath.test(profile.remote_data_dir) || invalidRemotePath(profile.remote_data_dir)) errors.push('Remote data directory must be a non-root absolute Linux path without dot segments.')
  if (!canonicalUuid.test(profile.workspace_id) || profile.workspace_id === '00000000-0000-0000-0000-000000000000') errors.push('Workspace ID must be a canonical non-zero UUID.')
  if (!Number.isInteger(profile.local_forward_port) || profile.local_forward_port < 1 || profile.local_forward_port > 65535) errors.push('Local port must be between 1 and 65535.')
  if (!Number.isInteger(profile.remote_port) || profile.remote_port < 1 || profile.remote_port > 65535) errors.push('Remote port must be between 1 and 65535.')
  return errors
}

function invalidRemotePath(value: string): boolean {
  return value === '/' || value.split('/').some((part) => part === '.' || part === '..')
}

function draftFor(mode: SsotStorageMode, profile: SsotRemoteProfile): SsotConnectionDraft {
  return mode === 'local' ? { storage_mode: 'local' } : { storage_mode: 'ssh-remote', ...profile }
}

function statusLabel(status: SsotConnectionStatus | null) {
  if (!status) return 'SSOT settings'
  if (status.state === 'testing') return 'Testing SSOT connection…'
  if (status.state === 'reconnecting') return 'Reconnecting remote SSOT…'
  if (status.state === 'disconnected') return 'Remote SSOT disconnected'
  if (status.restart_required) return 'SSOT restart required'
  if (status.state === 'error') return 'SSOT connection needs attention'
  return status.storage_mode === 'ssh-remote' ? 'Remote SSOT' : 'Local SSOT'
}

function needsAttention(status: SsotConnectionStatus | null): boolean {
  return status?.state === 'error' || status?.state === 'disconnected' || Boolean(status?.restart_required)
}

function RemoteProfileFields({ onChange, profile }: { onChange: (field: keyof SsotRemoteProfile, value: string) => void; profile: SsotRemoteProfile }) {
  return (
    <div className="ssot-remote-form">
      <label className="field"><span>SSH host alias <small>from your SSH config</small></span><input autoComplete="off" onChange={(event) => onChange('ssh_host_alias', event.target.value)} placeholder="work-linux" value={profile.ssh_host_alias} /></label>
      <label className="field"><span>Remote app directory</span><input onChange={(event) => onChange('remote_app_dir', event.target.value)} placeholder="/srv/workstack/app" value={profile.remote_app_dir} /></label>
      <label className="field"><span>Remote SSOT directory</span><input onChange={(event) => onChange('remote_data_dir', event.target.value)} placeholder="/srv/workstack/ssot" value={profile.remote_data_dir} /></label>
      <label className="field"><span>Workspace ID <small>from remote store-meta.json</small></span><input onChange={(event) => onChange('workspace_id', event.target.value)} placeholder="50d48f55-2cf3-4211-8a1e-5e668edaf622" value={profile.workspace_id} /></label>
      <div className="form-grid">
        <label className="field"><span>Local port</span><input min="1" max="65535" onChange={(event) => onChange('local_forward_port', event.target.value)} type="number" value={profile.local_forward_port} /></label>
        <label className="field"><span>Remote port</span><input min="1" max="65535" onChange={(event) => onChange('remote_port', event.target.value)} type="number" value={profile.remote_port} /></label>
      </div>
    </div>
  )
}

function ConnectionFields({ mode, onProfileChange, profile }: { mode: SsotStorageMode; onProfileChange: (field: keyof SsotRemoteProfile, value: string) => void; profile: SsotRemoteProfile }) {
  return mode === 'ssh-remote'
    ? <RemoteProfileFields onChange={onProfileChange} profile={profile} />
    : <p className="ssot-local-note">Work Stack will use its local protected workspace after restart. Existing remote data is not deleted or copied.</p>
}

function ConnectionFeedback({ errors, status, submitted }: { errors: string[]; status: SsotConnectionStatus | null; submitted: boolean }) {
  const failed = status?.state === 'error' || status?.state === 'disconnected'
  return <>
    {status?.message ? <p className={`ssot-connection-message ssot-connection-message--${status.state}`} role={failed ? 'alert' : 'status'}>{status.message}</p> : null}
    {submitted && errors.length ? <ul className="ssot-draft-errors" role="alert">{errors.map((error) => <li key={error}>{error}</li>)}</ul> : null}
    {status?.restart_required ? <p className="ssot-restart-notice">Settings were saved. Restart Work Stack to activate this connection.</p> : null}
    {status?.log_path ? <p className="ssot-log-path">Diagnostics: <code>{status.log_path}</code></p> : null}
  </>
}

function RemoteRuntimeFeedback({ busy, status }: { busy: boolean; status: SsotConnectionStatus | null }) {
  if (status?.storage_mode !== 'ssh-remote') return null
  const active = status.session_change_detection === true && !status.restart_required
  return (
    <section className="ssot-runtime-status" aria-label="Remote session status">
      <div>
        <strong>{active ? 'Live while Work Stack is open' : 'Starts after the saved connection is activated'}</strong>
        <small>
          {active
            ? `Remote SSOT changes are detected through the active session event stream${status.runtime_forward_port ? ` on local port ${status.runtime_forward_port}` : ''}.${status.remote_product_version ? ` Server ${status.remote_product_version}, protocol ${status.remote_protocol_version}.` : ''} No background daemon is installed.`
            : 'Change detection and reconnection run only during an active Work Stack desktop session.'}
        </small>
      </div>
      <div className="ssot-runtime-actions">
        <Button disabled={busy || status.restart_required} onClick={requestSsotReconnect}>Reconnect now</Button>
        {status.log_path ? <Button onClick={requestSsotDiagnostics} variant="ghost">Open diagnostics folder</Button> : null}
      </div>
    </section>
  )
}

function ModeChoice({ mode, onChange }: { mode: SsotStorageMode; onChange: (mode: SsotStorageMode) => void }) {
  return (
    <fieldset className="ssot-mode-choice">
      <legend>Workspace location</legend>
      <label className={mode === 'local' ? 'is-selected' : ''}>
        <input checked={mode === 'local'} name="ssot-mode" onChange={() => onChange('local')} type="radio" />
        <span><strong>Local workspace</strong><small>Keep the SSOT on this Windows device.</small></span>
      </label>
      <label className={mode === 'ssh-remote' ? 'is-selected' : ''}>
        <input checked={mode === 'ssh-remote'} name="ssot-mode" onChange={() => onChange('ssh-remote')} type="radio" />
        <span><strong>Remote SSH workspace</strong><small>Use a private Linux SSOT through a verified SSH tunnel.</small></span>
      </label>
    </fieldset>
  )
}

function ConnectionFooter({ busy, busyLabel, invalid, onCancel, onSubmit }: { busy: boolean; busyLabel: string; invalid: boolean; onCancel: () => void; onSubmit: (kind: 'test' | 'save') => void }) {
  return <>
    <Button onClick={onCancel} variant="ghost">Cancel</Button>
    <Button disabled={busy || invalid} onClick={() => onSubmit('test')}>{busy ? busyLabel : 'Test connection'}</Button>
    <Button disabled={busy || invalid} onClick={() => onSubmit('save')} variant="primary">Save settings</Button>
  </>
}

function sendValidatedDraft(kind: 'test' | 'save', mode: SsotStorageMode, profile: SsotRemoteProfile, errors: string[]) {
  if (errors.length) return
  const draft = draftFor(mode, profile)
  if (kind === 'test') requestSsotConnectionTest(draft)
  else saveSsotConnection(draft)
}

export function SsotConnectionCenter({ fallbackDetail = 'On this device · no background sync', fallbackLabel = 'Local workspace' }: { fallbackDetail?: string; fallbackLabel?: string }) {
  const supported = hasSsotHost()
  const [status, setStatus] = useState<SsotConnectionStatus | null>(null)
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState<SsotStorageMode>('local')
  const [profile, setProfile] = useState<SsotRemoteProfile>(emptyProfile)
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    if (!supported) return
    const unsubscribe = subscribeSsotConnectionStatus((next) => {
      setStatus(next)
      setMode(next.storage_mode)
      if (next.storage_mode === 'ssh-remote') setProfile({ ...emptyProfile, ...next.profile })
    })
    requestSsotConnectionStatus()
    return unsubscribe
  }, [supported])

  const errors = useMemo(() => validateSsotDraft(mode, profile), [mode, profile])
  if (!supported) return (
    <div className="sidebar-local">
      <span className="live-dot" />
      <div><strong>{fallbackLabel}</strong><small>{fallbackDetail}</small></div>
    </div>
  )
  const busy = status?.state === 'testing' || status?.state === 'reconnecting'
  const busyLabel = status?.state === 'reconnecting' ? 'Reconnecting…' : 'Testing…'
  const attention = needsAttention(status)
  const updateProfile = (field: keyof SsotRemoteProfile, value: string) => {
    setProfile((current) => ({
      ...current,
      [field]: field === 'local_forward_port' || field === 'remote_port' ? Number(value) : value,
    }))
  }
  const submit = (kind: 'test' | 'save') => {
    setSubmitted(true)
    sendValidatedDraft(kind, mode, profile, errors)
  }

  return (
    <>
      <button aria-label="Configure SSOT connection" className={`ssot-connection-control${attention ? ' ssot-connection-control--attention' : ''}`} onClick={() => setOpen(true)} type="button">
        <span className="ssot-connection-control__dot" />
        <span>{statusLabel(status)}</span>
      </button>
      <Dialog
        description="Choose where Work Stack keeps its authoritative workspace. Credentials and arbitrary SSH arguments are never accepted here."
        footer={<ConnectionFooter busy={busy} busyLabel={busyLabel} invalid={errors.length > 0} onCancel={() => setOpen(false)} onSubmit={submit} />}
        onClose={() => setOpen(false)}
        open={open}
        size="medium"
        title="SSOT connection"
      >
        <div className="ssot-connection-dialog">
          <ModeChoice mode={mode} onChange={(next) => { setMode(next); setSubmitted(false) }} />
          <ConnectionFields mode={mode} onProfileChange={updateProfile} profile={profile} />
          <ConnectionFeedback errors={errors} status={status} submitted={submitted} />
          <RemoteRuntimeFeedback busy={busy} status={status} />
        </div>
      </Dialog>
    </>
  )
}
