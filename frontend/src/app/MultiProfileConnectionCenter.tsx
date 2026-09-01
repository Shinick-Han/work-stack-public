import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'

import { Dialog } from '../components/Dialog'
import { Button, Pill } from '../components/Primitives'
import {
  CONNECTION_REGISTRY_SCHEMA_VERSION,
  connectionProfileDraftSchema,
  connectionProfileSchema,
  connectionRegistrySchema,
  type ConnectionProfile,
  type ConnectionProfileDraft,
  type ConnectionRegistry,
} from '../domain/connectionRegistrySchemas'
import {
  activateConnectionProfile,
  hasConnectionRegistryHost,
  requestConnectionProfileTest,
  requestConnectionRegistry,
  requestLocalDirectoryChoice,
  requestSshAliasDiscovery,
  saveConnectionRegistry,
  subscribeConnectionRegistryHostMessages,
  type ConnectionRegistryDigest,
  type ConnectionRegistryHostMessage,
} from './connectionRegistryHostBridge'

type Operation = Exclude<ConnectionRegistryHostMessage['operation'], null>
type SuccessMessage = Extract<ConnectionRegistryHostMessage, { ok: true }>
type TestResult = Extract<SuccessMessage, { operation: 'test-profile' }>['result']
type BeginRequest = (operation: Operation, send: (requestId: string) => string | null, fingerprint?: string) => void
interface PendingRequest { requestId: string; fingerprint?: string; timeoutId: number }

interface MultiProfileConnectionCenterProps {
  /** Separate dark gate: activation is a persisted restart transition, never a hot switch. */
  activationEnabled?: boolean
  enabled?: boolean
  open: boolean
  onClose: () => void
}

interface EditorContext {
  registry: ConnectionRegistry
  setRegistry: Dispatch<SetStateAction<ConnectionRegistry>>
  registryDigest: ConnectionRegistryDigest | undefined
  setRegistryDigest: Dispatch<SetStateAction<ConnectionRegistryDigest | undefined>>
  draft: ConnectionProfileDraft
  draftRef: { current: ConnectionProfileDraft }
  setDraft: Dispatch<SetStateAction<ConnectionProfileDraft>>
  originalFingerprint: string
  setOriginalFingerprint: Dispatch<SetStateAction<string>>
  aliases: readonly string[]
  setAliases: Dispatch<SetStateAction<readonly string[]>>
  testResult: TestResult | null
  setTestResult: Dispatch<SetStateAction<TestResult | null>>
  testedFingerprint: string | null
  setTestedFingerprint: Dispatch<SetStateAction<string | null>>
  testedRegistryDigest: ConnectionRegistryDigest | undefined
  setTestedRegistryDigest: Dispatch<SetStateAction<ConnectionRegistryDigest | undefined>>
  feedback: string
  setFeedback: Dispatch<SetStateAction<string>>
  error: string
  setError: Dispatch<SetStateAction<string>>
}

interface EditorActions {
  addProfile: (kind: 'local' | 'ssh') => void
  changeDraft: (draft: ConnectionProfileDraft) => void
  close: () => void
  persist: (activate: boolean) => void
  selectProfile: (profile: ConnectionProfile) => void
  testProfile: () => void
}

const emptyRegistry: ConnectionRegistry = {
  schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
  active_profile_id: null,
  profiles: [],
}

function createDraft(kind: 'local' | 'ssh'): ConnectionProfileDraft {
  const base = {
    profile_id: window.crypto.randomUUID(), label: '', enabled: true,
    live_updates: true, expected_workspace_id: null,
  }
  return kind === 'local'
    ? { ...base, kind, data_dir: '' }
    : { ...base, kind, ssh_host_alias: '', remote_app_dir: '', remote_data_dir: '', preferred_forward_port: 18_765, remote_port: 8_765 }
}

const fingerprint = (profile: ConnectionProfileDraft) => JSON.stringify({
  ...profile,
  label: profile.label.trim(),
})
const toDraft = (profile: ConnectionProfile): ConnectionProfileDraft => ({ ...profile })
const profilePath = (profile: ConnectionProfile) => profile.kind === 'local' ? profile.data_dir : `${profile.ssh_host_alias}:${profile.remote_data_dir}`
const pathName = (value: string) => value.replace(/[\\/]+$/, '').split(/[\\/]/).at(-1) || value

function authorityFingerprint(profile: ConnectionProfile | ConnectionProfileDraft): string {
  const authority = profile.kind === 'local'
    ? { kind: profile.kind, data_dir: profile.data_dir }
    : {
      kind: profile.kind,
      ssh_host_alias: profile.ssh_host_alias,
      remote_app_dir: profile.remote_app_dir,
      remote_data_dir: profile.remote_data_dir,
      preferred_forward_port: profile.preferred_forward_port,
      remote_port: profile.remote_port,
    }
  return JSON.stringify({ ...authority, enabled: profile.enabled, expected_workspace_id: profile.expected_workspace_id })
}

function replaceProfile(registry: ConnectionRegistry, profile: ConnectionProfile): readonly ConnectionProfile[] {
  return registry.profiles.some((candidate) => candidate.profile_id === profile.profile_id)
    ? registry.profiles.map((candidate) => candidate.profile_id === profile.profile_id ? profile : candidate)
    : [...registry.profiles, profile]
}

function testResultMessage(result: TestResult): string {
  if (result.status === 'identity_mismatch') return 'The detected workspace identity does not match this profile.'
  if (result.status === 'candidate') return 'This directory can become a new workspace only through a separately confirmed initialization flow.'
  return 'Connection test passed. The detected workspace identity is ready to save.'
}

function useEditorContext(): EditorContext {
  const [registry, setRegistry] = useState<ConnectionRegistry>(emptyRegistry)
  const [registryDigest, setRegistryDigest] = useState<ConnectionRegistryDigest | undefined>(undefined)
  const [draft, setDraft] = useState<ConnectionProfileDraft>(() => createDraft('local'))
  const draftRef = useRef(draft)
  const [originalFingerprint, setOriginalFingerprint] = useState(() => fingerprint(draft))
  const [aliases, setAliases] = useState<readonly string[]>([])
  const [testResult, setTestResult] = useState<TestResult | null>(null)
  const [testedFingerprint, setTestedFingerprint] = useState<string | null>(null)
  const [testedRegistryDigest, setTestedRegistryDigest] = useState<ConnectionRegistryDigest | undefined>(undefined)
  const [feedback, setFeedback] = useState('')
  const [error, setError] = useState('')
  draftRef.current = draft
  return {
    registry, setRegistry, registryDigest, setRegistryDigest, draft, draftRef, setDraft, originalFingerprint, setOriginalFingerprint,
    aliases, setAliases, testResult, setTestResult, testedFingerprint, setTestedFingerprint,
    testedRegistryDigest, setTestedRegistryDigest,
    feedback, setFeedback, error, setError,
  }
}

function applyLoadedRegistry(message: Extract<SuccessMessage, { operation: 'get-registry' }>, editor: EditorContext) {
  const registry = message.result.registry ?? emptyRegistry
  const selected = registry.profiles.find((profile) => profile.profile_id === registry.active_profile_id) ?? registry.profiles[0]
  const draft = selected ? toDraft(selected) : createDraft('local')
  editor.setRegistry(registry)
  editor.setRegistryDigest(message.result.registry_digest)
  editor.setDraft(draft)
  editor.setOriginalFingerprint(fingerprint(draft))
  editor.setTestResult(null)
  editor.setTestedFingerprint(null)
  editor.setTestedRegistryDigest(undefined)
  editor.setFeedback(registry.profiles.length ? 'Connection profiles loaded.' : 'Add your first SSOT connection profile.')
}

function applyTestResult(message: Extract<SuccessMessage, { operation: 'test-profile' }>, editor: EditorContext) {
  if (message.result.profile_id !== editor.draftRef.current.profile_id || message.result.kind !== editor.draftRef.current.kind) return
  const draft = message.result.actual_workspace_id && editor.draftRef.current.expected_workspace_id === null
    ? { ...editor.draftRef.current, expected_workspace_id: message.result.actual_workspace_id }
    : editor.draftRef.current
  editor.setDraft(draft)
  editor.setTestResult(message.result)
  editor.setTestedFingerprint(fingerprint(draft))
  editor.setFeedback(testResultMessage(message.result))
}

function applySavedRegistry(message: Extract<SuccessMessage, { operation: 'save-registry' | 'activate-profile' }>, editor: EditorContext) {
  editor.setRegistry(message.result.registry)
  editor.setRegistryDigest(message.result.registry_digest)
  const saved = message.result.registry.profiles.find((profile) => profile.profile_id === editor.draftRef.current.profile_id)
  if (saved) {
    const draft = toDraft(saved)
    editor.setDraft(draft)
    editor.setOriginalFingerprint(fingerprint(draft))
  }
  editor.setFeedback(message.operation === 'activate-profile'
    ? 'Profile saved. Restart Work Stack to activate this workspace.'
    : 'Profile saved. The running workspace has not changed.')
}

function applySuccessMessage(message: SuccessMessage, editor: EditorContext) {
  if (message.operation === 'get-registry') return applyLoadedRegistry(message, editor)
  if (message.operation === 'discover-ssh-aliases') {
    editor.setAliases(message.result.aliases)
    editor.setFeedback(message.result.aliases.length ? 'SSH aliases loaded from your SSH config.' : 'No safe SSH aliases were found. You may enter one manually.')
    return
  }
  if (message.operation === 'choose-local-directory') {
    if (message.result.selection !== null) editor.setDraft((draft) => draft.kind === 'local'
      ? {
          ...draft,
          data_dir: message.result.selection ?? draft.data_dir,
          label: draft.label.trim() || pathName(message.result.selection ?? draft.data_dir),
        }
      : draft)
    editor.setTestResult(null)
    editor.setTestedFingerprint(null)
    return
  }
  if (message.operation === 'test-profile') return applyTestResult(message, editor)
  applySavedRegistry(message, editor)
}

function useRegistryHost(enabled: boolean, open: boolean, editor: EditorContext) {
  const editorRef = useRef(editor)
  const pendingRef = useRef(new Map<Operation, PendingRequest>())
  const [pendingOperations, setPendingOperations] = useState<ReadonlySet<Operation>>(new Set())
  editorRef.current = editor
  function finish(operation: Operation) {
    const pending = pendingRef.current.get(operation)
    if (pending) window.clearTimeout(pending.timeoutId)
    pendingRef.current.delete(operation)
    setPendingOperations(new Set(pendingRef.current.keys()))
  }
  const begin: BeginRequest = (operation, send, candidateFingerprint) => {
    const requestId = window.crypto.randomUUID()
    const timeoutId = window.setTimeout(() => {
      const pending = pendingRef.current.get(operation)
      if (!pending || pending.requestId !== requestId) return
      finish(operation)
      editorRef.current.setError('The connection operation timed out. Try again.')
    }, 20_000)
    pendingRef.current.set(operation, { requestId, fingerprint: candidateFingerprint, timeoutId })
    setPendingOperations(new Set(pendingRef.current.keys()))
    try {
      if (send(requestId) === null) {
        finish(operation)
        editorRef.current.setError('The native Work Stack connection service is unavailable.')
      }
    } catch (caught) {
      finish(operation)
      editorRef.current.setError(caught instanceof Error ? caught.message : 'The connection request was rejected.')
    }
  }
  useEffect(() => {
    if (!enabled || !open) return
    const unsubscribe = subscribeConnectionRegistryHostMessages((message) => {
      if (message.operation === null) return
      const pending = pendingRef.current.get(message.operation)
      if (!pending || pending.requestId !== message.request_id) return
      if (pending.fingerprint !== undefined && pending.fingerprint !== fingerprint(editorRef.current.draftRef.current)) {
        finish(message.operation)
        return
      }
      finish(message.operation)
      if (!message.ok) return editorRef.current.setError(message.error.message)
      editorRef.current.setError('')
      applySuccessMessage(message, editorRef.current)
    })
    if (hasConnectionRegistryHost()) begin('get-registry', requestConnectionRegistry)
    else editorRef.current.setError('Multi-profile connections require the Work Stack desktop app.')
    return () => {
      unsubscribe()
      pendingRef.current.forEach((pending) => window.clearTimeout(pending.timeoutId))
      pendingRef.current.clear()
      setPendingOperations(new Set())
    }
  }, [enabled, open])
  return { begin, pendingOperations }
}

function resetEditor(editor: EditorContext, draft: ConnectionProfileDraft) {
  editor.setDraft(draft)
  editor.setOriginalFingerprint(fingerprint(draft))
  editor.setTestResult(null)
  editor.setTestedFingerprint(null)
  editor.setTestedRegistryDigest(undefined)
  editor.setFeedback('')
  editor.setError('')
}

function useEditorActions(editor: EditorContext, begin: BeginRequest, onClose: () => void): EditorActions {
  const dirty = fingerprint(editor.draft) !== editor.originalFingerprint
  const tested = editor.testedFingerprint === fingerprint(editor.draft) && editor.testResult?.status === 'ready'
  const validDraft = connectionProfileDraftSchema.safeParse(editor.draft)
  const profile = connectionProfileSchema.safeParse(editor.draft)
  const confirmDiscard = () => !dirty || window.confirm('Discard unsaved connection profile changes?')
  function addProfile(kind: 'local' | 'ssh') {
    if (!confirmDiscard()) return
    resetEditor(editor, createDraft(kind))
    if (kind === 'ssh') begin('discover-ssh-aliases', requestSshAliasDiscovery)
  }
  function changeDraft(draft: ConnectionProfileDraft) {
    editor.setDraft(draft)
    editor.setTestResult(null)
    editor.setTestedFingerprint(null)
    editor.setTestedRegistryDigest(undefined)
    editor.setFeedback('')
  }
  function selectProfile(selected: ConnectionProfile) {
    if (confirmDiscard()) resetEditor(editor, toDraft(selected))
  }
  function testProfile() {
    const baseRegistryDigest = editor.registryDigest
    if (validDraft.success && baseRegistryDigest !== undefined) {
      editor.setTestedRegistryDigest(baseRegistryDigest)
      begin('test-profile', (id) => requestConnectionProfileTest(validDraft.data, baseRegistryDigest, id), fingerprint(validDraft.data))
    }
  }
  function persist(activate: boolean) {
    if (!profile.success || !tested) return
    if (editor.registryDigest === undefined) {
      editor.setError('Profile writes require a CAS-capable desktop host. Update Work Stack and reopen this center.')
      return
    }
    const expectedRegistryDigest = editor.registryDigest
    const candidate = connectionRegistrySchema.safeParse({
      schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
      active_profile_id: activate ? profile.data.profile_id : editor.registry.active_profile_id,
      profiles: replaceProfile(editor.registry, profile.data),
    })
    if (!candidate.success) return editor.setError(editor.registry.profiles.length === 0 && !activate
      ? 'Your first profile must be saved and activated together.' : 'This profile conflicts with the current connection registry.')
    const candidateFingerprint = fingerprint(editor.draft)
    if (activate) {
      const proofId = editor.testResult?.proof_id
      if (!proofId || editor.testedRegistryDigest !== editor.registryDigest) {
        editor.setError('Test this exact profile against the current registry before scheduling activation.')
        return
      }
      begin('activate-profile', (id) => activateConnectionProfile(candidate.data, profile.data.profile_id, proofId, expectedRegistryDigest, id), candidateFingerprint)
    } else begin('save-registry', (id) => saveConnectionRegistry(candidate.data, expectedRegistryDigest, id), candidateFingerprint)
  }
  return { addProfile, changeDraft, close: () => { if (confirmDiscard()) onClose() }, persist, selectProfile, testProfile }
}

function ProfileList({ activeId, draftId, pending, profiles, onAdd, onSelect }: {
  activeId: string | null; draftId: string; pending: boolean; profiles: readonly ConnectionProfile[]
  onAdd: (kind: 'local' | 'ssh') => void; onSelect: (profile: ConnectionProfile) => void
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
        <span>{activeId === profile.profile_id ? 'Active' : 'Inactive'} · {profile.enabled ? 'Enabled' : 'Disabled'} · {pathName(profilePath(profile))}</span>
      </button>
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
    <Button disabled={pending} onClick={() => begin('discover-ssh-aliases', requestSshAliasDiscovery)}>Refresh SSH aliases</Button>
    <label className="field"><span>Remote app directory</span><input onChange={(event) => onChange({ ...draft, remote_app_dir: event.target.value })} value={draft.remote_app_dir} /></label>
    <label className="field"><span>Remote SSOT directory</span><input onChange={(event) => onChange({ ...draft, remote_data_dir: event.target.value })} value={draft.remote_data_dir} /></label>
    <details><summary>Advanced ports</summary><div className="form-grid">
      <label className="field"><span>Preferred local port</span><input max="65535" min="1" onChange={(event) => onChange({ ...draft, preferred_forward_port: Number(event.target.value) })} type="number" value={draft.preferred_forward_port} /></label>
      <label className="field"><span>Remote port</span><input max="65535" min="1" onChange={(event) => onChange({ ...draft, remote_port: Number(event.target.value) })} type="number" value={draft.remote_port} /></label>
    </div></details>
  </div>
}

function DetectedIdentity({ draft, result }: { draft: ConnectionProfileDraft; result: TestResult | null }) {
  const identity = result?.actual_workspace_id ?? draft.expected_workspace_id ?? 'Run Test connection'
  const version = result?.product_version
  const protocol = result?.protocol_version
  return <div aria-live="polite" className="multi-profile-connections__identity">
    <strong>Detected workspace identity</strong><code>{identity}</code>
    {version ? <span>Work Stack {version}</span> : null}
    {protocol === null || protocol === undefined ? null : <span>Protocol {protocol}</span>}
  </div>
}

function ProfileEditor({ actions, begin, editor, pending }: { actions: EditorActions; begin: BeginRequest; editor: EditorContext; pending: ReadonlySet<Operation> }) {
  if (pending.has('get-registry')) return <section aria-labelledby="connection-profile-editor-title"><h3 id="connection-profile-editor-title">Loading profiles</h3><p role="status">Reading the connection registry…</p></section>
  if (pending.has('save-registry') || pending.has('activate-profile')) return <section aria-labelledby="connection-profile-editor-title"><h3 id="connection-profile-editor-title">Saving profile</h3><p role="status">The editor is locked until the correlated native response arrives.</p></section>
  const draftValid = connectionProfileDraftSchema.safeParse(editor.draft).success
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
    <DetectedIdentity draft={editor.draft} result={editor.testResult} />
    <Button disabled={pending.size > 0 || !draftValid} onClick={actions.testProfile}>Test connection</Button>
    {editor.feedback ? <p aria-live="polite" role="status">{editor.feedback}</p> : null}
    {editor.error ? <p role="alert">{editor.error}</p> : null}
    {dirty ? <p aria-live="polite">Unsaved changes</p> : null}
  </section>
}

function connectionCenterActions(editor: EditorContext, activationEnabled: boolean, pending: boolean) {
  const tested = editor.testedFingerprint === fingerprint(editor.draft) && editor.testResult?.status === 'ready'
  const profileValid = connectionProfileSchema.safeParse(editor.draft).success
  const original = editor.registry.profiles.find((profile) => profile.profile_id === editor.draft.profile_id)
  const activeProfile = editor.registry.active_profile_id === editor.draft.profile_id
  const authorityChanged = original === undefined ? false : authorityFingerprint(original) !== authorityFingerprint(editor.draft)
  const saveAllowed = [
    editor.registryDigest !== undefined, !pending, tested, profileValid,
    editor.registry.profiles.length > 0, !(activeProfile && authorityChanged),
  ].every(Boolean)
  const activationAllowed = [
    activationEnabled, editor.registryDigest !== undefined, !pending, tested, profileValid,
    editor.testResult?.proof_id, editor.testedRegistryDigest === editor.registryDigest,
  ].every(Boolean)
  return { activationAllowed, saveAllowed }
}

/** Feature-gated connection center. App keeps the legacy center mounted unless this gate is explicit. */
export function MultiProfileConnectionCenter({ activationEnabled = false, enabled = false, onClose, open }: MultiProfileConnectionCenterProps) {
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
      <ProfileList activeId={editor.registry.active_profile_id} draftId={editor.draft.profile_id} onAdd={actions.addProfile} onSelect={actions.selectProfile} pending={host.pendingOperations.size > 0} profiles={editor.registry.profiles} />
      <ProfileEditor actions={actions} begin={host.begin} editor={editor} pending={host.pendingOperations} />
    </div>
  </Dialog>
}
