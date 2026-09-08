import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'

import {
  CONNECTION_REGISTRY_SCHEMA_VERSION,
  connectionProfileDraftSchema,
  remotePythonExecutableSchema,
  type ConnectionProfile,
  type ConnectionProfileDraft,
  type ConnectionProfileWrite,
  type ConnectionRegistry,
} from '../domain/connectionRegistrySchemas'
import {
  hasConnectionRegistryHost,
  requestConnectionRegistry,
  subscribeConnectionRegistryHostMessages,
  type ConnectionRegistryDigest,
  type ConnectionRegistryHostMessage,
} from './connectionRegistryHostBridge'

export type Operation = Exclude<ConnectionRegistryHostMessage['operation'], null>
type SuccessMessage = Extract<ConnectionRegistryHostMessage, { ok: true }>
export type TestResult = Extract<SuccessMessage, { operation: 'test-profile' }>['result']
/**
 * What a reply is allowed to write.
 *
 * - `editor` writes the draft, its proof or the saved profile, so it is fenced
 *   by the editor generation it was issued against.
 * - `registry-recovery` is the one automatic reload after a failed or
 *   unanswered activation. It writes only the registry snapshot and its digest,
 *   never the draft and never the invalidated proof, so it stays valid across
 *   an authored edit under its own owner and request ID. Rejecting it would
 *   strand the reload status instead of protecting anything.
 * - `ambient` results (SSH alias lists) touch no editor state at all.
 */
export type RequestPurpose = 'editor' | 'registry-recovery' | 'ambient'
export type BeginRequest = (
  operation: Operation,
  send: (requestId: string) => string | null,
  fingerprint?: string,
  purpose?: RequestPurpose,
) => void
export interface PendingRequest {
  requestId: string
  /** The editor lifetime that issued this request: one open/mount of the center. */
  owner: number
  /** The editor generation at the moment this request left. */
  generation: number
  purpose: RequestPurpose
  fingerprint?: string
  timeoutId: number
}
export interface EditorError { code: string; message: string; pid?: number }
export interface ActivationPreview {
  kind: 'same-authority' | 'different-authority'
  current: ConnectionProfile
  next: ConnectionProfileWrite
  candidate: ConnectionRegistry
  proofId: string
  expectedRegistryDigest: ConnectionRegistryDigest
}

const HOST_ERROR_SUMMARIES = {
  ssh_auth_failed: 'SSH authentication failed.',
  remote_python_required: 'A remote Python executable path is required.',
  remote_python_not_found: 'The remote Python executable was not found.',
  remote_python_too_old: 'The remote Python interpreter is too old.',
  remote_app_mismatch: 'The remote app directory is not a matching Work Stack release.',
  remote_workspace_mismatch: 'The remote workspace identity does not match.',
  remote_lock_owned: 'The remote workspace is owned by another live session.',
  remote_protocol_invalid: 'The remote protocol response is invalid.',
  ssh_test_failed: 'The SSH profile could not be verified.',
  ssh_test_unavailable: 'SSH profile testing is not available in this desktop session.',
  registry_conflict: 'The connection registry changed. Reload and try again.',
  test_required: 'Test this exact profile against the current registry before scheduling activation.',
  activation_ambiguous: 'Several unconfirmed activations match this profile. This activation was refused and nothing was written.',
  activation_conflict: 'Another unconfirmed activation targets this connection state with an unknown outcome. This activation was refused and nothing was written.',
  activation_unconfirmed: 'An earlier connection activation is still unconfirmed. This activation was refused and nothing was written.',
  activation_manual_review: 'The stored connection activation records need manual review. This activation was refused and nothing was written.',
  operation_failed: 'Connection registry operation failed.',
  invalid_request: 'Connection registry request is invalid.',
} as const

type HostErrorCode = keyof typeof HOST_ERROR_SUMMARIES

export interface EditorContext {
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
  error: EditorError | null
  setError: Dispatch<SetStateAction<EditorError | null>>
  activationPreview: ActivationPreview | null
  setActivationPreview: Dispatch<SetStateAction<ActivationPreview | null>>
  // The one removal this component is waiting for. A save-registry reply is a
  // deletion receipt only when it answers exactly this request.
  removalRef: { current: PendingRemoval | null }
  // The one automatic snapshot reload this component is waiting for after an
  // activation failed or went unanswered. A get-registry reply refreshes the
  // registry without touching the authored draft only when it answers exactly
  // this request, and only one such reload is ever outstanding.
  refreshRef: { current: string | null }
  // Explicit editor identity. `owner` names one open/mount lifetime of this
  // center; `generation` increments on every authored edit, reset, profile
  // selection and close. Draft bytes are not identity: an A -> B -> A edit
  // returns the same fingerprint under a later generation, so a Test or Save
  // reply issued against the earlier generation can never be revived.
  ownerRef: { current: number }
  generationRef: { current: number }
}

export interface PendingRemoval {
  label: string
  path: string
  profileId: string
  requestId: string
}

export const emptyRegistry: ConnectionRegistry = {
  schema_version: CONNECTION_REGISTRY_SCHEMA_VERSION,
  active_profile_id: null,
  profiles: [],
}

export function createDraft(kind: 'local' | 'ssh'): ConnectionProfileDraft {
  const base = {
    profile_id: window.crypto.randomUUID(), label: '', enabled: true,
    live_updates: true, expected_workspace_id: null,
  }
  return kind === 'local'
    ? { ...base, kind, data_dir: '' }
    : { ...base, kind, ssh_host_alias: '', remote_app_dir: '', remote_data_dir: '', preferred_forward_port: 18_765, remote_port: 8_765, remote_python: '' }
}

export const fingerprint = (profile: ConnectionProfileDraft) => JSON.stringify({
  ...profile,
  label: profile.label.trim(),
})
export const toDraft = (profile: ConnectionProfile): ConnectionProfileDraft => (
  profile.kind === 'ssh' ? { ...profile, remote_python: profile.remote_python ?? '' } : { ...profile }
)
export const profilePath = (profile: ConnectionProfile) => profile.kind === 'local' ? profile.data_dir : `${profile.ssh_host_alias}:${profile.remote_data_dir}`
export const pathName = (value: string) => value.replace(/[\\/]+$/, '').split(/[\\/]/).at(-1) || value

function allowlistedPid(details: { readonly pid?: number } | undefined): number | undefined {
  const pid = details?.pid
  if (typeof pid !== 'number' || !Number.isInteger(pid) || pid < 1 || pid > 4_294_967_295) return undefined
  return pid
}

export function presentHostError(code: string, details: { readonly pid?: number } | undefined): EditorError {
  const pid = allowlistedPid(details)
  const presented: EditorError = Object.hasOwn(HOST_ERROR_SUMMARIES, code)
    ? { code, message: HOST_ERROR_SUMMARIES[code as HostErrorCode] }
    : { code: 'ssh_test_failed', message: HOST_ERROR_SUMMARIES.ssh_test_failed }
  return pid === undefined ? presented : { ...presented, pid }
}

/**
 * Retire the current editor generation. Every authored edit, reset, profile
 * selection and close lifecycle step invalidates the editor state that any
 * outstanding editor-affecting request was issued against, whatever the draft
 * bytes happen to be when the reply lands.
 */
export function nextEditorGeneration(editor: EditorContext) {
  editor.generationRef.current += 1
}

/**
 * Whether a correlated reply may no longer write the editor. Fingerprint
 * equality stays as an exact-candidate check, but it cannot substitute for the
 * generation: it is the generation that survives an A -> B -> A edit.
 */
export function isStaleForEditor(pending: PendingRequest, editor: EditorContext): boolean {
  if (pending.purpose !== 'editor') return false
  if (pending.generation !== editor.generationRef.current) return true
  return pending.fingerprint !== undefined && pending.fingerprint !== fingerprint(editor.draftRef.current)
}

export function invalidateProof(editor: EditorContext) {
  editor.setTestResult(null)
  editor.setTestedFingerprint(null)
  editor.setTestedRegistryDigest(undefined)
}

/**
 * An unanswered activation is not a known failure. The desktop service may have
 * written the registry, so this states the uncertainty and claims nothing else.
 */
const ACTIVATION_UNKNOWN_ERROR: EditorError = {
  code: 'operation_failed',
  message: 'The activation result is unknown: the desktop service did not answer, so Work Stack cannot tell whether this profile was activated.',
}

const ACTIVATION_REFRESH_FEEDBACK = 'Reloading the connection registry…'
const ACTIVATION_REFRESHED_FEEDBACK = 'The connection registry was reloaded. Test this profile again before activating.'
const ACTIVATION_REFRESH_FAILED_FEEDBACK = 'The connection registry was not reloaded. Close and reopen this center before activating.'

export function useEditorContext(): EditorContext {
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
  const [error, setError] = useState<EditorError | null>(null)
  const [activationPreview, setActivationPreview] = useState<ActivationPreview | null>(null)
  const removalRef = useRef<PendingRemoval | null>(null)
  const refreshRef = useRef<string | null>(null)
  const ownerRef = useRef(0)
  const generationRef = useRef(0)
  draftRef.current = draft
  return {
    registry, setRegistry, registryDigest, setRegistryDigest, draft, draftRef, setDraft, originalFingerprint, setOriginalFingerprint,
    aliases, setAliases, testResult, setTestResult, testedFingerprint, setTestedFingerprint,
    testedRegistryDigest, setTestedRegistryDigest,
    feedback, setFeedback, error, setError, activationPreview, setActivationPreview, removalRef, refreshRef,
    ownerRef, generationRef,
  }
}

function applyLoadedRegistry(message: Extract<SuccessMessage, { operation: 'get-registry' }>, editor: EditorContext) {
  const registry = message.result.registry ?? emptyRegistry
  const selected = registry.profiles.find((profile) => profile.profile_id === registry.active_profile_id) ?? registry.profiles[0]
  const draft = selected ? toDraft(selected) : createDraft('local')
  nextEditorGeneration(editor)
  editor.setRegistry(registry)
  editor.setRegistryDigest(message.result.registry_digest)
  editor.setDraft(draft)
  editor.setOriginalFingerprint(fingerprint(draft))
  editor.setTestResult(null)
  editor.setTestedFingerprint(null)
  editor.setTestedRegistryDigest(undefined)
  editor.setActivationPreview(null)
  editor.setError(null)
  editor.setFeedback(registry.profiles.length ? 'Connection profiles loaded.' : 'Add your first SSOT connection profile.')
}

export function releaseRecovery(editor: EditorContext, requestId: string) {
  // Only the exact request that could not be sent is released. A newer reload,
  // or an outcome that already completed, keeps the correlation it holds.
  if (editor.refreshRef.current === requestId) editor.refreshRef.current = null
}

/**
 * One automatic snapshot reload after an activation failed or went unanswered.
 * It is never an activation retry, and never more than one reload in flight:
 * the proof is already invalidated, so the reloaded digest can only be reached
 * again through a fresh Test and an explicit activation.
 */
function refreshAfterActivation(editor: EditorContext, begin: BeginRequest) {
  if (editor.refreshRef.current !== null) return
  const attempt = { sent: false }
  begin('get-registry', (requestId) => {
    // Armed before the bridge call: a host that answers re-entrantly, before
    // requestConnectionRegistry returns, must still take the recovery path
    // instead of the initial-load path that replaces the authored editor.
    editor.refreshRef.current = requestId
    try {
      const sent = requestConnectionRegistry(requestId)
      if (sent === null) {
        // Null after a correlated reply is completion, not an unsent read.
        if (editor.refreshRef.current === requestId) releaseRecovery(editor, requestId)
        else attempt.sent = true
        return sent
      }
      attempt.sent = true
      return sent
    } catch (caught) {
      if (editor.refreshRef.current === requestId) releaseRecovery(editor, requestId)
      else attempt.sent = true
      throw caught
    }
  }, undefined, 'registry-recovery')
  if (!attempt.sent) {
    editor.setFeedback(ACTIVATION_REFRESH_FAILED_FEEDBACK)
    return
  }
  // A reply already delivered re-entrantly owns the status it wrote.
  if (editor.refreshRef.current !== null) editor.setFeedback(ACTIVATION_REFRESH_FEEDBACK)
}

/**
 * The reply to that one reload. It replaces only the registry snapshot: the
 * authored draft, its original fingerprint and the activation failure stay, and
 * the invalidated proof is not restored, so this cannot resurrect an expired
 * proof or overwrite a profile the user typed while the reload was in flight.
 */
function applyRefreshedRegistry(message: Extract<SuccessMessage, { operation: 'get-registry' }>, editor: EditorContext) {
  editor.refreshRef.current = null
  editor.setRegistry(message.result.registry ?? emptyRegistry)
  editor.setRegistryDigest(message.result.registry_digest)
  editor.setFeedback(ACTIVATION_REFRESHED_FEEDBACK)
}

function testResultMessage(result: TestResult): string {
  if (result.status === 'identity_mismatch') return 'The detected workspace identity does not match this profile.'
  if (result.status === 'candidate') return 'This directory can become a new workspace only through a separately confirmed initialization flow.'
  return 'Connection test passed. The detected workspace identity is ready to save.'
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

export function resetEditor(editor: EditorContext, draft: ConnectionProfileDraft) {
  nextEditorGeneration(editor)
  editor.setDraft(draft)
  editor.setOriginalFingerprint(fingerprint(draft))
  editor.setTestResult(null)
  editor.setTestedFingerprint(null)
  editor.setTestedRegistryDigest(undefined)
  editor.setFeedback('')
  editor.setError(null)
  editor.setActivationPreview(null)
}

export function applyAuthoredEdit(editor: EditorContext, draft: ConnectionProfileDraft) {
  nextEditorGeneration(editor)
  editor.setDraft(draft)
  editor.setTestResult(null)
  editor.setTestedFingerprint(null)
  editor.setTestedRegistryDigest(undefined)
  editor.setActivationPreview(null)
  editor.setFeedback('')
}

function applyRemovedProfile(
  message: Extract<SuccessMessage, { operation: 'save-registry' }>,
  editor: EditorContext,
  removal: PendingRemoval,
) {
  editor.removalRef.current = null
  const registry = message.result.registry
  editor.setRegistry(registry)
  editor.setRegistryDigest(message.result.registry_digest)
  if (registry.profiles.some((profile) => profile.profile_id === removal.profileId)) {
    // The host answered this exact request but the entry survived. Report that
    // rather than claiming a removal that did not happen.
    editor.setError({ code: 'operation_failed', message: 'The saved connection profile was not removed. Reload the connection registry and try again.' })
    return
  }
  const surviving = registry.profiles.find((profile) => profile.profile_id === registry.active_profile_id)
    ?? registry.profiles[0]
  resetEditor(editor, surviving ? toDraft(surviving) : createDraft('local'))
  editor.setFeedback(`Removed the saved connection profile “${removal.label}”. Its SSOT at ${removal.path} was not touched and the running workspace has not changed.`)
}

function applySavedRegistry(message: Extract<SuccessMessage, { operation: 'save-registry' | 'activate-profile' }>, editor: EditorContext) {
  const removal = editor.removalRef.current
  if (message.operation === 'save-registry' && removal !== null && removal.requestId === message.request_id) {
    return applyRemovedProfile(message, editor, removal)
  }
  editor.setRegistry(message.result.registry)
  editor.setRegistryDigest(message.result.registry_digest)
  editor.setActivationPreview(null)
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

function applyFailureMessage(
  message: Extract<ConnectionRegistryHostMessage, { ok: false }>,
  editor: EditorContext,
  begin: BeginRequest,
) {
  if (editor.removalRef.current?.requestId === message.request_id) editor.removalRef.current = null
  const mapped = presentHostError(message.error.code, message.error.details)
  if (editor.refreshRef.current === message.request_id) {
    // The recovery read is secondary. The activation refusal that started it
    // stays the primary alert, exactly as the recovery-timeout branch does, so
    // a failed and an unanswered reload report the same cause.
    editor.refreshRef.current = null
    editor.setFeedback(ACTIVATION_REFRESH_FAILED_FEEDBACK)
    return
  }
  const activation = message.operation === 'activate-profile'
  if (activation || mapped.code === 'registry_conflict' || mapped.code === 'test_required' || message.operation === 'test-profile') {
    invalidateProof(editor)
  }
  // The refusal is reported after the reload starts, so a reload that cannot be
  // sent never replaces the activation outcome the user has to read.
  if (activation) refreshAfterActivation(editor, begin)
  editor.setError(mapped)
}

function applyRequestTimeout(editor: EditorContext, operation: Operation, requestId: string, begin: BeginRequest) {
  if (editor.refreshRef.current === requestId) {
    editor.refreshRef.current = null
    editor.setFeedback(ACTIVATION_REFRESH_FAILED_FEEDBACK)
    return
  }
  if (editor.removalRef.current?.requestId === requestId) {
    editor.removalRef.current = null
    editor.setError({ code: 'operation_failed', message: 'The removal result is unknown: the desktop service did not answer. Reload the connection registry before trying again.' })
    return
  }
  if (operation === 'activate-profile') {
    invalidateProof(editor)
    refreshAfterActivation(editor, begin)
    editor.setError(ACTIVATION_UNKNOWN_ERROR)
    return
  }
  editor.setError({ code: 'operation_failed', message: 'The connection operation timed out. Try again.' })
}

function applyHostMessage(message: ConnectionRegistryHostMessage, editor: EditorContext, begin: BeginRequest) {
  if (!message.ok) return applyFailureMessage(message, editor, begin)
  if (message.operation === 'get-registry' && editor.refreshRef.current === message.request_id) {
    return applyRefreshedRegistry(message, editor)
  }
  editor.setError(null)
  applySuccessMessage(message, editor)
}

export function useRegistryHost(enabled: boolean, open: boolean, editor: EditorContext) {
  const editorRef = useRef(editor)
  const pendingRef = useRef(new Map<Operation, PendingRequest>())
  const [pendingOperations, setPendingOperations] = useState<ReadonlySet<Operation>>(new Set())
  editorRef.current = editor
  function finish(operation: Operation, requestId: string): boolean {
    // Only the exact live request is retired. A finished reply, or a newer
    // request of the same operation, keeps the slot it now holds.
    const pending = pendingRef.current.get(operation)
    if (!pending || pending.requestId !== requestId) return false
    window.clearTimeout(pending.timeoutId)
    pendingRef.current.delete(operation)
    setPendingOperations(new Set(pendingRef.current.keys()))
    return true
  }
  const begin: BeginRequest = (operation, send, candidateFingerprint, purpose = 'editor') => {
    const requestId = window.crypto.randomUUID()
    const owner = editorRef.current.ownerRef.current
    const generation = editorRef.current.generationRef.current
    const timeoutId = window.setTimeout(() => {
      const pending = pendingRef.current.get(operation)
      if (!pending || pending.requestId !== requestId) return
      if (!finish(operation, requestId)) return
      if (pending.owner !== editorRef.current.ownerRef.current) return
      if (isStaleForEditor(pending, editorRef.current)) return
      applyRequestTimeout(editorRef.current, operation, requestId, begin)
    }, 20_000)
    pendingRef.current.set(operation, { requestId, owner, generation, purpose, fingerprint: candidateFingerprint, timeoutId })
    setPendingOperations(new Set(pendingRef.current.keys()))
    try {
      if (send(requestId) === null) {
        if (finish(operation, requestId)) {
          editorRef.current.setError({ code: 'operation_failed', message: 'The native Work Stack connection service is unavailable.' })
        }
      }
    } catch (caught) {
      if (finish(operation, requestId)) {
        editorRef.current.setError({
          code: 'operation_failed',
          message: caught instanceof Error ? caught.message.slice(0, 256) : 'The connection request was rejected.',
        })
      }
    }
  }
  useEffect(() => {
    if (!enabled || !open) return
    editorRef.current.ownerRef.current += 1
    const unsubscribe = subscribeConnectionRegistryHostMessages((message) => {
      if (message.operation === null) return
      const pending = pendingRef.current.get(message.operation)
      if (!pending || pending.requestId !== message.request_id) return
      // A stale reply still finishes its own pending slot, so the editor is
      // never left locked on a request nobody will answer again. It simply
      // never writes the newer editor it no longer belongs to.
      finish(message.operation, message.request_id)
      if (pending.owner !== editorRef.current.ownerRef.current) return
      if (isStaleForEditor(pending, editorRef.current)) return
      applyHostMessage(message, editorRef.current, begin)
    })
    if (hasConnectionRegistryHost()) begin('get-registry', requestConnectionRegistry)
    else editorRef.current.setError({ code: 'operation_failed', message: 'Multi-profile connections require the Work Stack desktop app.' })
    return () => {
      unsubscribe()
      pendingRef.current.forEach((pending) => window.clearTimeout(pending.timeoutId))
      pendingRef.current.clear()
      editorRef.current.refreshRef.current = null
      editorRef.current.ownerRef.current += 1
      editorRef.current.generationRef.current += 1
      setPendingOperations(new Set())
    }
  }, [enabled, open])
  return { begin, pendingOperations }
}
