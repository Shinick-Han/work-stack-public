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
  const busy = status?.state === 'checking' || status?.state === 'downloading' || status?.state === 'installing'
  const attention = status && ['available', 'ready', 'blocked', 'error'].includes(status.state)
  const label = status?.state === 'ready'
    ? `Update ${status.latest_version} ready`
    : status?.state === 'available'
      ? `Update ${status.latest_version} available`
      : status?.state === 'downloading'
        ? `Downloading ${status.latest_version}`
        : status?.state === 'error'
          ? 'Update check failed'
          : `Work Stack updates${status ? ` · ${status.current_version}` : ''}`

  const updatePreferences = (changes: Partial<UpdatePreferences>) => {
    if (!status) return
    saveUpdatePreferences({ ...status.preferences, ...changes })
  }

  return (
    <>
      {attention || busy ? (
        <button className={`update-status update-status--${status?.state}`} onClick={() => setOpen(true)} type="button">
          <span className="update-status__dot" />{label}
        </button>
      ) : (
        <IconButton icon="refresh" label={label} onClick={() => setOpen(true)} variant="ghost" />
      )}
      <Dialog
        description="Updates are downloaded from the stable Work Stack GitHub release channel and verified before installation."
        footer={<>
          {status?.release_url ? <Button onClick={openUpdateRelease} variant="ghost">Release notes</Button> : null}
          <Button disabled={busy} icon="refresh" onClick={requestUpdateCheck}>Check now</Button>
          {status?.state === 'available' ? <Button disabled={busy} onClick={requestUpdateDownload}>Download update</Button> : null}
          {status?.state === 'ready' ? <Button onClick={requestUpdateInstall} variant="primary">Install and restart</Button> : null}
        </>}
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
          {status ? <fieldset>
            <legend>Automatic updates</legend>
            <label><input checked={status.preferences.auto_check} onChange={(event) => updatePreferences({ auto_check: event.target.checked })} type="checkbox" /><span><strong>Check automatically</strong><small>Check once when Work Stack starts.</small></span></label>
            <label><input checked={status.preferences.auto_download} onChange={(event) => updatePreferences({ auto_download: event.target.checked })} type="checkbox" /><span><strong>Download automatically</strong><small>Download only a bounded, verified Work Stack release.</small></span></label>
            <label><input checked={status.preferences.install_on_exit} onChange={(event) => updatePreferences({ install_on_exit: event.target.checked })} type="checkbox" /><span><strong>Install when Work Stack closes</strong><small>Apply the verified update after local and SSH processes stop.</small></span></label>
          </fieldset> : null}
          {status?.state === 'error' || status?.state === 'blocked' ? <p className="update-dialog__warning" role="alert">{status.message}</p> : null}
        </div>
      </Dialog>
    </>
  )
}
