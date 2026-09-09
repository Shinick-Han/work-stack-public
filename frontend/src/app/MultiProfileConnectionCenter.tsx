import { Dialog } from '../components/Dialog'
import { Button, Pill } from '../components/Primitives'
import {
  CONNECTION_REGISTRY_SCHEMA_VERSION,
  connectionProfileDraftSchema,
  connectionProfileWriteSchema,
  connectionRegistrySchema,
  remotePythonExecutableSchema,
  sshProfileNeedsRemotePython,
  type ConnectionProfile,
  type ConnectionProfileDraft,
  type ConnectionProfileWrite,
  type ConnectionRegistry,
} from '../domain/connectionRegistrySchemas'
import {
  type ActivationPreview,
  type BeginRequest,
  type EditorContext,
  type EditorError,
  type Operation,
  type TestResult,
  applyAuthoredEdit,
  createDraft,
  fingerprint,
  invalidateProof,
  nextEditorGeneration,
  pathName,
  profilePath,
  resetEditor,
  toDraft,
  useEditorContext,
  useRegistryHost,
} from './connectionCenterHost'
import {
  activateConnectionProfile,
  requestConnectionProfileTest,
  requestLocalDirectoryChoice,
  requestSshAliasDiscovery,
  saveConnectionRegistry,
  type ConnectionRegistryDigest,
} from './connectionRegistryHostBridge'
import './MultiProfileConnectionCenter.css'

interface MultiProfileConnectionCenterProps {
  /** Separate dark gate: activation is a persisted restart transition, never a hot switch. */
  activationEnabled?: boolean
  enabled?: boolean
  onReviewSynchronization?: () => void
  open: boolean
  onClose: () => void
}

interface EditorActions {
  addProfile: (kind: 'local' | 'ssh') => void
  changeDraft: (draft: ConnectionProfileDraft) => void
  close: () => void
  persist: (activate: boolean) => void
  confirmActivation: () => void
  cancelActivation: () => void
  removeProfile: (profile: ConnectionProfile) => void
  selectProfile: (profile: ConnectionProfile) => void
  testProfile: () => void
}

export function authorityFingerprint(profile: ConnectionProfile | ConnectionProfileDraft): string {
  const authority = profile.kind === 'local'
    ? { kind: profile.kind, data_dir: profile.data_dir }
    : {
      kind: profile.kind,
      ssh_host_alias: profile.ssh_host_alias,
      remote_app_dir: profile.remote_app_dir,
      remote_data_dir: profile.remote_data_dir,
      preferred_forward_port: profile.preferred_forward_port,
      remote_port: profile.remote_port,
      remote_python: profile.remote_python ?? '',
      knowledge_drivers_config: profile.knowledge_drivers_config ?? '',
    }
  return JSON.stringify({ ...authority, enabled: profile.enabled, expected_workspace_id: profile.expected_workspace_id })
}

function replaceProfile(registry: ConnectionRegistry, profile: ConnectionProfile): readonly ConnectionProfile[] {
  return registry.profiles.some((candidate) => candidate.profile_id === profile.profile_id)
    ? registry.profiles.map((candidate) => candidate.profile_id === profile.profile_id ? profile : candidate)
    : [...registry.profiles, profile]
}

function testableDraft(draft: ConnectionProfileDraft) {
  const parsed = connectionProfileDraftSchema.safeParse(draft)
  if (!parsed.success) return null
  if (parsed.data.kind === 'ssh' && !remotePythonExecutableSchema.safeParse(parsed.data.remote_python).success) {
    return null
  }
  return parsed.data
}

function buildActivationRegistry(
  registry: ConnectionRegistry,
  next: ConnectionProfileWrite,
  mode: 'same-authority' | 'different-authority',
): ConnectionRegistry {
  const disableSameAuthority = (profile: ConnectionProfile): ConnectionProfile => (
    mode === 'same-authority'
      && profile.profile_id !== next.profile_id
      && profile.enabled
      && profile.expected_workspace_id === next.expected_workspace_id
      ? { ...profile, enabled: false }
      : profile
  )
  const profiles = registry.profiles.some((profile) => profile.profile_id === next.profile_id)
    ? registry.profiles.map((profile) => (
        profile.profile_id === next.profile_id ? next : disableSameAuthority(profile)
      ))
    : [...registry.profiles.map(disableSameAuthority), next]
  return {
    schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
    active_profile_id: next.profile_id,
    profiles,
  }
}

export type PersistDecision =
  | { readonly action: 'unavailable' }
  | { readonly action: 'error'; readonly error: EditorError; readonly invalidateProof: boolean }
  | { readonly action: 'save'; readonly candidate: ConnectionRegistry; readonly expectedRegistryDigest: ConnectionRegistryDigest }
  | { readonly action: 'preview'; readonly preview: ActivationPreview }
  | { readonly action: 'activate'; readonly candidate: ConnectionRegistry; readonly next: ConnectionProfileWrite; readonly proofId: string; readonly expectedRegistryDigest: ConnectionRegistryDigest }

const CAS_UNAVAILABLE_ERROR: EditorError = {
  code: 'operation_failed',
  message: 'Profile writes require a CAS-capable desktop host. Update Work Stack and reopen this center.',
}

const TEST_REQUIRED_ERROR: EditorError = {
  code: 'test_required',
  message: 'Test this exact profile against the current registry before scheduling activation.',
}

export function activationReplacementKind(
  active: ConnectionProfile | undefined,
  next: ConnectionProfileWrite,
): 'same-authority' | 'different-authority' | 'none' {
  if (active === undefined || active.profile_id === next.profile_id) return 'none'
  if (active.expected_workspace_id === next.expected_workspace_id) return 'same-authority'
  return 'different-authority'
}

export function registryWriteConflictError(
  registry: ConnectionRegistry,
  emptyRegistryUsesOperationFailed: boolean,
): EditorError {
  const empty = registry.profiles.length === 0
  return {
    code: empty && emptyRegistryUsesOperationFailed ? 'operation_failed' : 'registry_conflict',
    message: empty
      ? 'Your first profile must be saved and activated together.'
      : 'This profile conflicts with the current connection registry.',
  }
}

export function decideMetadataSave(
  registry: ConnectionRegistry,
  registryDigest: ConnectionRegistryDigest | undefined,
  next: ConnectionProfileWrite,
): PersistDecision {
  if (registryDigest === undefined) return { action: 'unavailable' }
  const candidate = connectionRegistrySchema.safeParse({
    schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
    active_profile_id: registry.active_profile_id,
    profiles: replaceProfile(registry, next),
  })
  if (!candidate.success) {
    return { action: 'error', error: registryWriteConflictError(registry, true), invalidateProof: false }
  }
  return { action: 'save', candidate: candidate.data, expectedRegistryDigest: registryDigest }
}

export function decideActivationPersist(
  registry: ConnectionRegistry,
  registryDigest: ConnectionRegistryDigest | undefined,
  next: ConnectionProfileWrite,
  proofId: string | undefined,
  testedRegistryDigest: ConnectionRegistryDigest | undefined,
): PersistDecision {
  if (registryDigest === undefined) return { action: 'unavailable' }
  if (!proofId || testedRegistryDigest !== registryDigest) {
    return { action: 'error', error: TEST_REQUIRED_ERROR, invalidateProof: true }
  }
  const active = registry.profiles.find((profile) => profile.profile_id === registry.active_profile_id)
  const kind = activationReplacementKind(active, next)
  const mode = kind === 'same-authority' ? 'same-authority' : 'different-authority'
  const candidate = connectionRegistrySchema.safeParse(buildActivationRegistry(registry, next, mode))
  if (!candidate.success) {
    return { action: 'error', error: registryWriteConflictError(registry, false), invalidateProof: false }
  }
  if (kind === 'none' || active === undefined) {
    return {
      action: 'activate',
      candidate: candidate.data,
      next,
      proofId,
      expectedRegistryDigest: registryDigest,
    }
  }
  return {
    action: 'preview',
    preview: {
      kind,
      current: active,
      next,
      candidate: candidate.data,
      proofId,
      expectedRegistryDigest: registryDigest,
    },
  }
}

function applyPersistDecision(
  editor: EditorContext,
  begin: BeginRequest,
  decision: PersistDecision,
  draftFingerprint: string,
) {
  if (decision.action === 'unavailable') {
    editor.setError(CAS_UNAVAILABLE_ERROR)
    return
  }
  if (decision.action === 'error') {
    editor.setError(decision.error)
    if (decision.invalidateProof) invalidateProof(editor)
    return
  }
  if (decision.action === 'save') {
    begin('save-registry', (id) => saveConnectionRegistry(decision.candidate, decision.expectedRegistryDigest, id), draftFingerprint)
    return
  }
  if (decision.action === 'preview') {
    editor.setActivationPreview(decision.preview)
    return
  }
  begin(
    'activate-profile',
    (id) => activateConnectionProfile(
      decision.candidate,
      decision.next.profile_id,
      decision.proofId,
      decision.expectedRegistryDigest,
      id,
    ),
    draftFingerprint,
  )
}

function useEditorActions(editor: EditorContext, begin: BeginRequest, onClose: () => void): EditorActions {
  const dirty = fingerprint(editor.draft) !== editor.originalFingerprint
  const tested = editor.testedFingerprint === fingerprint(editor.draft) && editor.testResult?.status === 'ready'
  const readyDraft = testableDraft(editor.draft)
  const written = connectionProfileWriteSchema.safeParse(editor.draft)
  const confirmDiscard = () => !dirty || window.confirm('Discard unsaved connection profile changes?')
  function addProfile(kind: 'local' | 'ssh') {
    if (!confirmDiscard()) return
    resetEditor(editor, createDraft(kind))
    if (kind === 'ssh') begin('discover-ssh-aliases', requestSshAliasDiscovery, undefined, 'ambient')
  }
  const changeDraft = (draft: ConnectionProfileDraft) => applyAuthoredEdit(editor, draft)
  function selectProfile(selected: ConnectionProfile) {
    if (confirmDiscard()) resetEditor(editor, toDraft(selected))
  }
  function removeProfile(target: ConnectionProfile) {
    const persisted = editor.registry.profiles.find((profile) => profile.profile_id === target.profile_id)
    if (!persisted) {
      editor.setError({ code: 'operation_failed', message: 'Only a saved connection profile can be removed.' })
      return
    }
    if (persisted.profile_id === editor.registry.active_profile_id) {
      editor.setError({ code: 'operation_failed', message: 'The active connection profile cannot be removed. Activate another profile first.' })
      return
    }
    if (editor.registryDigest === undefined) {
      editor.setError({ code: 'operation_failed', message: 'Profile writes require a CAS-capable desktop host. Update Work Stack and reopen this center.' })
      return
    }
    const path = profilePath(persisted)
    const confirmed = window.confirm(
      `Remove the saved connection profile “${persisted.label}” (${path})?\n\n`
      + `Only this connection entry is removed. The SSOT at ${path} and everything inside it are left untouched.`,
    )
    if (!confirmed) return
    if (dirty && !window.confirm('Your unsaved connection profile changes will be discarded. Remove the saved profile anyway?')) return
    const expectedRegistryDigest = editor.registryDigest
    const candidate = connectionRegistrySchema.safeParse({
      schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
      active_profile_id: editor.registry.active_profile_id,
      profiles: editor.registry.profiles.filter((profile) => profile.profile_id !== persisted.profile_id),
    })
    if (!candidate.success) {
      editor.setError({ code: 'registry_conflict', message: 'This removal conflicts with the current connection registry.' })
      return
    }
    editor.setError(null)
    editor.setFeedback('')
    begin('save-registry', (id) => {
      editor.removalRef.current = { label: persisted.label, path, profileId: persisted.profile_id, requestId: id }
      return saveConnectionRegistry(candidate.data, expectedRegistryDigest, id)
    })
  }
  function testProfile() {
    const baseRegistryDigest = editor.registryDigest
    if (readyDraft !== null && baseRegistryDigest !== undefined) {
      editor.setTestedRegistryDigest(baseRegistryDigest)
      begin('test-profile', (id) => requestConnectionProfileTest(readyDraft, baseRegistryDigest, id), fingerprint(readyDraft))
    }
  }
  function sendActivation(preview: ActivationPreview) {
    const candidateFingerprint = fingerprint(editor.draft)
    begin(
      'activate-profile',
      (id) => activateConnectionProfile(
        preview.candidate,
        preview.next.profile_id,
        preview.proofId,
        preview.expectedRegistryDigest,
        id,
      ),
      candidateFingerprint,
    )
  }
  function persist(activate: boolean) {
    if (!written.success || !tested) return
    const next: ConnectionProfileWrite = activate ? { ...written.data, enabled: true } : written.data
    const decision = activate
      ? decideActivationPersist(
          editor.registry,
          editor.registryDigest,
          next,
          editor.testResult?.proof_id ?? undefined,
          editor.testedRegistryDigest,
        )
      : decideMetadataSave(editor.registry, editor.registryDigest, next)
    applyPersistDecision(editor, begin, decision, fingerprint(editor.draft))
  }
  function confirmActivation() {
    const preview = editor.activationPreview
    if (preview === null) return
    editor.setActivationPreview(null)
    sendActivation(preview)
  }
  function cancelActivation() {
    editor.setActivationPreview(null)
  }
  return {
    addProfile, changeDraft, close: () => { if (confirmDiscard()) { nextEditorGeneration(editor); onClose() } }, persist,
    confirmActivation, cancelActivation, removeProfile, selectProfile, testProfile,
  }
}

function ProfileList({ activeId, draftId, pending, profiles, onAdd, onRemove, onSelect }: {
  activeId: string | null; draftId: string; pending: boolean; profiles: readonly ConnectionProfile[]
  onAdd: (kind: 'local' | 'ssh') => void; onRemove: (profile: ConnectionProfile) => void; onSelect: (profile: ConnectionProfile) => void
}) {
  return <section aria-labelledby="connection-profile-list-title">
    <div className="multi-profile-connections__heading"><h3 id="connection-profile-list-title">Workspace profiles</h3><div>
      <Button disabled={pending} onClick={() => onAdd('local')}>Add local</Button>
      <Button disabled={pending} onClick={() => onAdd('ssh')}>Add SSH</Button>
    </div></div>
    {profiles.length ? <ul className="connection-profile-list">{profiles.map((profile) => <li key={profile.profile_id}>
      <button aria-current={profile.profile_id === draftId ? 'true' : undefined} className="connection-profile-list__item" onClick={() => onSelect(profile)} type="button">
        <span><strong>{profile.label}</strong> <Pill>{profile.kind === 'local' ? 'Local' : 'SSH'}</Pill></span>
        <span>{profilePath(profile)}</span>
        <span>{activeId === profile.profile_id ? 'Active' : 'Inactive'} · {profile.enabled ? 'Enabled' : 'Disabled'} · {pathName(profilePath(profile))}{sshProfileNeedsRemotePython(profile) ? ' · Needs Remote Python' : ''}</span>
      </button>
      {activeId === profile.profile_id
        ? null
        : <Button disabled={pending} onClick={() => onRemove(profile)}>{`Remove ${profile.label}`}</Button>}
    </li>)}</ul> : <p>No connection profiles have been saved.</p>}
  </section>
}

function ProfileKindChoice({ kind, onChange }: { kind: 'local' | 'ssh'; onChange: (kind: 'local' | 'ssh') => void }) {
  return <fieldset className="ssot-mode-choice"><legend>Connection type</legend>
    <label className={kind === 'local' ? 'is-selected' : ''}><input checked={kind === 'local'} name="profile-kind" onChange={() => onChange('local')} type="radio" /><span><strong>Local SSOT</strong><small>A protected directory on this device.</small></span></label>
    <label className={kind === 'ssh' ? 'is-selected' : ''}><input checked={kind === 'ssh'} name="profile-kind" onChange={() => onChange('ssh')} type="radio" /><span><strong>Remote SSH SSOT</strong><small>A fixed SSH config alias and remote directories.</small></span></label>
  </fieldset>
}

function LocalFields({ begin, draft, onChange, pending }: { begin: BeginRequest; draft: Extract<ConnectionProfileDraft, { kind: 'local' }>; onChange: (draft: ConnectionProfileDraft) => void; pending: boolean }) {
  return <div className="multi-profile-connections__path-row">
    <label className="field"><span>Local SSOT directory</span><input onChange={(event) => onChange({ ...draft, data_dir: event.target.value })} value={draft.data_dir} /></label>
    <Button disabled={pending} onClick={() => begin('choose-local-directory', requestLocalDirectoryChoice, fingerprint(draft))}>Browse…</Button>
  </div>
}

function SshFields({ aliases, begin, draft, onChange, pending }: { aliases: readonly string[]; begin: BeginRequest; draft: Extract<ConnectionProfileDraft, { kind: 'ssh' }>; onChange: (draft: ConnectionProfileDraft) => void; pending: boolean }) {
  return <div className="ssot-remote-form">
    <label className="field"><span>SSH host alias</span><input autoComplete="off" list="workstack-ssh-aliases" onChange={(event) => onChange({ ...draft, ssh_host_alias: event.target.value })} value={draft.ssh_host_alias} /></label>
    <datalist id="workstack-ssh-aliases">{aliases.map((alias) => <option key={alias} value={alias} />)}</datalist>
    <Button disabled={pending} onClick={() => begin('discover-ssh-aliases', requestSshAliasDiscovery, undefined, 'ambient')}>Refresh SSH aliases</Button>
    <label className="field"><span>Remote app directory</span><input onChange={(event) => onChange({ ...draft, remote_app_dir: event.target.value })} value={draft.remote_app_dir} /></label>
    <label className="field"><span>Remote SSOT directory</span><input onChange={(event) => onChange({ ...draft, remote_data_dir: event.target.value })} value={draft.remote_data_dir} /></label>
    <label className="field"><span>Remote Python executable</span><input aria-required="true" autoComplete="off" onChange={(event) => onChange({ ...draft, remote_python: event.target.value })} spellCheck={false} value={draft.remote_python} /></label>
    <details><summary>Advanced ports</summary><div className="form-grid">
      <label className="field"><span>Preferred local port</span><input max="65535" min="1" onChange={(event) => onChange({ ...draft, preferred_forward_port: Number(event.target.value) })} type="number" value={draft.preferred_forward_port} /></label>
      <label className="field"><span>Remote port</span><input max="65535" min="1" onChange={(event) => onChange({ ...draft, remote_port: Number(event.target.value) })} type="number" value={draft.remote_port} /></label>
    </div></details>
  </div>
}

function DetectedIdentity({ draft, onReviewSynchronization, result }: {
  draft: ConnectionProfileDraft
  onReviewSynchronization?: () => void
  result: TestResult | null
}) {
  const identity = result?.actual_workspace_id ?? draft.expected_workspace_id ?? 'Run Test connection'
  const version = result?.product_version
  const protocol = result?.protocol_version
  const mismatch = result?.status === 'identity_mismatch'
    && result.actual_workspace_id !== null
    && draft.expected_workspace_id !== null
    && result.actual_workspace_id !== draft.expected_workspace_id
  return <div aria-live="polite" className="multi-profile-connections__identity">
    {mismatch ? <>
      <strong>Saved profile identity</strong><code>{draft.expected_workspace_id}</code>
      <strong>Detected workspace identity</strong><code>{result.actual_workspace_id}</code>
      <p role="alert">These identities differ. Activation is blocked because this screen cannot prove whether the detected identity is a durable workspace authority or an unresolved Store synchronization candidate. Review the workspace synchronization status before changing this profile.</p>
      {onReviewSynchronization ? <Button onClick={onReviewSynchronization}>Review workspace synchronization</Button> : null}
    </> : <><strong>Detected workspace identity</strong><code>{identity}</code></>}
    {version ? <span>Work Stack {version}</span> : null}
    {protocol === null || protocol === undefined ? null : <span>Protocol {protocol}</span>}
  </div>
}

function ProfileEditorStatus({ title, message }: { title: string; message: string }) {
  return <section aria-labelledby="connection-profile-editor-title"><h3 id="connection-profile-editor-title">{title}</h3><p role="status">{message}</p></section>
}

function ActivationPreviewPanel({ preview, onCancel, onConfirm }: {
  preview: ActivationPreview
  onCancel: () => void
  onConfirm: () => void
}) {
  return <section aria-labelledby="activation-preview-title" className="multi-profile-connections__preview">
    <h3 id="activation-preview-title">{preview.kind === 'same-authority'
      ? 'Replace the active workspace after restart'
      : 'Activate a different workspace after restart'}</h3>
    <dl>
      <div>
        <dt>Current profile</dt>
        <dd><strong>{preview.current.label}</strong> <Pill>{preview.current.kind === 'local' ? 'Local' : 'SSH'}</Pill> <code>{preview.current.expected_workspace_id}</code></dd>
      </div>
      <div>
        <dt>Next profile</dt>
        <dd><strong>{preview.next.label}</strong> <Pill>{preview.next.kind === 'local' ? 'Local' : 'SSH'}</Pill> <code>{preview.next.expected_workspace_id}</code></dd>
      </div>
    </dl>
    <p>Work Stack will not switch now. Restart is required after this save.</p>
    <div className="multi-profile-connections__preview-actions">
      <Button onClick={onCancel}>Cancel</Button>
      <Button onClick={onConfirm} variant="primary">Confirm save and activate after restart</Button>
    </div>
  </section>
}

function HostErrorAlert({ error }: { error: EditorError }) {
  return <>
    <p role="alert"><code>{error.code}</code> {error.message}</p>
    {error.pid === undefined ? null : <details>
      <summary>Details</summary>
      <p><code>{error.code}</code> PID {error.pid}</p>
    </details>}
  </>
}

function ProfileEditorForm({ actions, begin, editor, onReviewSynchronization, pending }: {
  actions: EditorActions
  begin: BeginRequest
  editor: EditorContext
  onReviewSynchronization?: () => void
  pending: ReadonlySet<Operation>
}) {
  const draftValid = testableDraft(editor.draft) !== null
  const dirty = fingerprint(editor.draft) !== editor.originalFingerprint
  return <section aria-labelledby="connection-profile-editor-title">
    <h3 id="connection-profile-editor-title">{editor.registry.profiles.some((profile) => profile.profile_id === editor.draft.profile_id) ? 'Edit profile' : 'Add profile'}</h3>
    <ProfileKindChoice kind={editor.draft.kind} onChange={actions.addProfile} />
    <label className="field"><span>Profile label</span><input maxLength={100} onChange={(event) => actions.changeDraft({ ...editor.draft, label: event.target.value })} value={editor.draft.label} /></label>
    <label className="multi-profile-connections__check"><input checked={editor.draft.enabled} disabled={editor.registry.active_profile_id === editor.draft.profile_id} onChange={(event) => actions.changeDraft({ ...editor.draft, enabled: event.target.checked })} type="checkbox" /> Enabled</label>
    <label className="multi-profile-connections__check"><input checked={editor.draft.live_updates} onChange={(event) => actions.changeDraft({ ...editor.draft, live_updates: event.target.checked })} type="checkbox" /> Watch for changes while Work Stack is open</label>
    {editor.draft.kind === 'local'
      ? <LocalFields begin={begin} draft={editor.draft} onChange={actions.changeDraft} pending={pending.has('choose-local-directory')} />
      : <SshFields aliases={editor.aliases} begin={begin} draft={editor.draft} onChange={actions.changeDraft} pending={pending.has('discover-ssh-aliases')} />}
    <DetectedIdentity draft={editor.draft} onReviewSynchronization={onReviewSynchronization} result={editor.testResult} />
    {editor.draft.kind === 'ssh' && sshProfileNeedsRemotePython(editor.draft)
      ? <p className="multi-profile-connections__python-required" role="status">Remote Python executable is required. Enter an explicit absolute path, then Test before Save or Activate. Work Stack never guesses an interpreter from PATH.</p>
      : null}
    <Button disabled={pending.size > 0 || !draftValid} onClick={actions.testProfile}>Test connection</Button>
    {editor.feedback ? <p aria-live="polite" role="status">{editor.feedback}</p> : null}
    {editor.error ? <HostErrorAlert error={editor.error} /> : null}
    {dirty ? <p aria-live="polite">Unsaved changes</p> : null}
  </section>
}

function ProfileEditor({ actions, begin, editor, onReviewSynchronization, pending }: {
  actions: EditorActions
  begin: BeginRequest
  editor: EditorContext
  onReviewSynchronization?: () => void
  pending: ReadonlySet<Operation>
}) {
  // A read the user asked for replaces the editor. The automatic reload after a
  // failed activation does not: it keeps the authored draft and the activation
  // outcome on screen, and its controls are already disabled while it is in
  // flight. The ref is only read here, and it holds one value for the whole
  // lifetime of that reload.
  if (pending.has('get-registry') && editor.refreshRef.current === null) {
    return <ProfileEditorStatus title="Loading profiles" message="Reading the connection registry…" />
  }
  if (pending.has('save-registry') || pending.has('activate-profile')) return <ProfileEditorStatus title="Saving profile" message="The editor is locked until the correlated native response arrives." />
  if (editor.activationPreview) {
    return <ActivationPreviewPanel preview={editor.activationPreview} onCancel={actions.cancelActivation} onConfirm={actions.confirmActivation} />
  }
  return <ProfileEditorForm actions={actions} begin={begin} editor={editor} onReviewSynchronization={onReviewSynchronization} pending={pending} />
}

function connectionCenterActions(editor: EditorContext, activationEnabled: boolean, pending: boolean) {
  const tested = editor.testedFingerprint === fingerprint(editor.draft) && editor.testResult?.status === 'ready'
  const profileValid = connectionProfileWriteSchema.safeParse(editor.draft).success
  const original = editor.registry.profiles.find((profile) => profile.profile_id === editor.draft.profile_id)
  const activeProfile = editor.registry.active_profile_id === editor.draft.profile_id
  const authorityChanged = original === undefined ? false : authorityFingerprint(original) !== authorityFingerprint(editor.draft)
  const previewOpen = editor.activationPreview !== null
  const saveAllowed = [
    editor.registryDigest !== undefined, !pending, tested, profileValid, !previewOpen,
    editor.registry.profiles.length > 0, !(activeProfile && authorityChanged),
  ].every(Boolean)
  const activationAllowed = [
    activationEnabled, editor.registryDigest !== undefined, !pending, tested, profileValid, !previewOpen,
    editor.testResult?.proof_id, editor.testedRegistryDigest === editor.registryDigest,
  ].every(Boolean)
  return { activationAllowed, saveAllowed }
}

/** Feature-gated connection center. App keeps the legacy center mounted unless this gate is explicit. */
export function MultiProfileConnectionCenter({ activationEnabled = false, enabled = false, onClose, onReviewSynchronization, open }: MultiProfileConnectionCenterProps) {
  const editor = useEditorContext()
  const host = useRegistryHost(enabled, open, editor)
  const actions = useEditorActions(editor, host.begin, onClose)
  const available = connectionCenterActions(editor, activationEnabled, host.pendingOperations.size > 0)
  if (!enabled) return null
  return <Dialog description="Configure connection metadata here. Work Stack never deletes, copies, or merges an SSOT directory from this screen."
    footer={<><Button onClick={actions.close} variant="ghost">Close</Button>
      <Button disabled={!available.saveAllowed} onClick={() => actions.persist(false)}>Save profile</Button>
      <Button disabled={!available.activationAllowed} onClick={() => actions.persist(true)} variant="primary">Save and activate after restart</Button></>}
    onClose={actions.close} open={open} size="large" title="SSOT connections">
    <div className="multi-profile-connections">
      <ProfileList activeId={editor.registry.active_profile_id} draftId={editor.draft.profile_id} onAdd={actions.addProfile} onRemove={actions.removeProfile} onSelect={actions.selectProfile} pending={host.pendingOperations.size > 0} profiles={editor.registry.profiles} />
      <ProfileEditor actions={actions} begin={host.begin} editor={editor} onReviewSynchronization={onReviewSynchronization} pending={host.pendingOperations} />
    </div>
  </Dialog>
}
