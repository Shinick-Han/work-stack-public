import { useEffect, useState } from 'react'
import { Dialog } from '../components/Dialog'
import { Button, IconButton } from '../components/Primitives'
import {
  hasUpdateHost,
  openUpdateRelease,
  requestUpdateCheck,
  requestUpdateDownload,
  requestUpdateInstall,
  requestUpdateStatus,
  saveUpdatePreferences,
  subscribeUpdateStatus,
  type UpdateHostStatus,
  type UpdatePreferences,
} from './updateHostBridge'

interface UpdatePresentation {
  attention: boolean
  busy: boolean
  label: string
}

const busyStates = new Set(['checking', 'downloading', 'installing'])
const attentionStates = new Set(['available', 'ready', 'blocked', 'error'])

export function updateStatusPresentation(status: UpdateHostStatus | null): UpdatePresentation {
  const state = status?.state
  const labels: Partial<Record<UpdateHostStatus['state'], string>> = {
    ready: `Update ${status?.latest_version} ready`,
    available: `Update ${status?.latest_version} available`,
    downloading: `Downloading ${status?.latest_version}`,
    error: 'Update check failed',
  }
  return {
    attention: state ? attentionStates.has(state) : false,
    busy: state ? busyStates.has(state) : false,
    label: (state ? labels[state] : undefined) ?? `Work Stack updates${status ? ` · ${status.current_version}` : ''}`,
  }
}

function UpdateTrigger({ onOpen, presentation, state }: {
  onOpen: () => void
  presentation: UpdatePresentation
  state?: UpdateHostStatus['state']
}) {
  if (!presentation.attention && !presentation.busy) {
    return <IconButton icon="refresh" label={presentation.label} onClick={onOpen} variant="ghost" />
  }
  return (
    <button className={`update-status update-status--${state}`} onClick={onOpen} type="button">
      <span className="update-status__dot" />{presentation.label}
    </button>
  )
}

function UpdateDialogFooter({ busy, status }: { busy: boolean; status: UpdateHostStatus | null }) {
  return <>
    {status?.release_url ? <Button onClick={openUpdateRelease} variant="ghost">Release notes</Button> : null}
    <Button disabled={busy} icon="refresh" onClick={requestUpdateCheck}>Check now</Button>
    {status?.state === 'available' ? <Button disabled={busy} onClick={requestUpdateDownload}>Download update</Button> : null}
    {status?.state === 'ready' ? <Button onClick={requestUpdateInstall} variant="primary">Install and restart</Button> : null}
  </>
}

function UpdatePreferencesForm({ onUpdate, status }: {
  onUpdate: (changes: Partial<UpdatePreferences>) => void
  status: UpdateHostStatus | null
}) {
  if (!status) return null
  return <fieldset>
    <legend>Automatic updates</legend>
    <label><input checked={status.preferences.auto_check} onChange={(event) => onUpdate({ auto_check: event.target.checked })} type="checkbox" /><span><strong>Check automatically</strong><small>Check once when Work Stack starts.</small></span></label>
    <label><input checked={status.preferences.auto_download} onChange={(event) => onUpdate({ auto_download: event.target.checked })} type="checkbox" /><span><strong>Download automatically</strong><small>Download only a bounded, verified Work Stack release.</small></span></label>
    <label><input checked={status.preferences.install_on_exit} onChange={(event) => onUpdate({ install_on_exit: event.target.checked })} type="checkbox" /><span><strong>Install when Work Stack closes</strong><small>Apply the verified update after local and SSH processes stop.</small></span></label>
  </fieldset>
}

function UpdateWarning({ status }: { status: UpdateHostStatus | null }) {
  if (status?.state !== 'error' && status?.state !== 'blocked') return null
  return <p className="update-dialog__warning" role="alert">{status.message}</p>
}

export function UpdateStatusControl() {
  const [status, setStatus] = useState<UpdateHostStatus | null>(null)
  const [open, setOpen] = useState(false)
  const supported = hasUpdateHost()

  useEffect(() => {
    if (!supported) return
    const unsubscribe = subscribeUpdateStatus(setStatus)
    requestUpdateStatus()
    return unsubscribe
  }, [supported])

  if (!supported) return null
  const presentation = updateStatusPresentation(status)

  const updatePreferences = (changes: Partial<UpdatePreferences>) => {
    if (!status) return
    saveUpdatePreferences({ ...status.preferences, ...changes })
  }

  return (
    <>
      <UpdateTrigger onOpen={() => setOpen(true)} presentation={presentation} state={status?.state} />
      <Dialog
        description="Updates are downloaded from the stable Work Stack GitHub release channel and verified before installation."
        footer={<UpdateDialogFooter busy={presentation.busy} status={status} />}
        onClose={() => setOpen(false)}
        open={open}
        size="small"
        title="Work Stack updates"
      >
        <div className="update-dialog">
          <dl>
            <div><dt>Installed</dt><dd>{status?.current_version ?? 'Detecting…'}</dd></div>
            <div><dt>Latest</dt><dd>{status?.latest_version || 'Checking…'}</dd></div>
            <div><dt>Status</dt><dd>{status?.message || 'Ready to check the stable channel.'}</dd></div>
          </dl>
          <UpdatePreferencesForm onUpdate={updatePreferences} status={status} />
          <UpdateWarning status={status} />
        </div>
      </Dialog>
    </>
  )
}
