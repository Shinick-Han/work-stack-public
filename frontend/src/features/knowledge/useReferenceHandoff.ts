import { useCallback, useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'

import { api } from '../../api/client'
import { copyTextToClipboard } from '../../utils/clipboard'
import { getErrorMessage } from '../../utils/format'
import { isKnowledgeCancelled, KnowledgeHostError, knowledgeErrorMessage } from './knowledgeErrors'
import { knowledgeHostAvailable, requestKnowledge } from './knowledgeHostBridge'
import type { KnowledgeBinding, KnowledgeSavedReference, KnowledgeTaskRef, KnowledgeVault } from './knowledgeTypes'
import {
  resumeProgressChanged,
  UNBOUND_RESUME_PROGRESS,
  type ResumeProgressFacts,
} from './resumeProgressContract'
import {
  allChangedAcknowledged,
  downloadKnowledgeContextJson,
  handoffBinding,
  knowledgeContextFilename,
  nextHandoffSelection,
  prepareReferenceHandoff,
  ReferenceHandoffError,
  type PreparedReferenceHandoff,
} from './referenceHandoff'

const STATUS_TIMEOUT_MS = 15_000
const HOST_UNAVAILABLE = 'Open this Task in the Work Stack desktop app to export local references.'
export const STALE_PROGRESS_COPY =
  'Recorded progress changed after this brief was prepared. Prepare the brief again before copying.'

export interface ReferenceHandoffState {
  acknowledged: string[]
  briefCopied: boolean
  copied: boolean
  error: string | null
  loading: boolean
  pending: 'list' | 'prepare' | 'copy' | null
  prepared: PreparedReferenceHandoff | null
  references: KnowledgeSavedReference[]
}

/**
 * A reference list already loaded for this exact binding by the surrounding panel. It
 * exists so the resume subview does not run a second `list-references` against the same
 * binding; preparation still re-lists through the host before exporting anything.
 */
export interface ReferenceHandoffSeed {
  references: readonly KnowledgeSavedReference[]
  /** Changes when the seeded records change, which restarts the selection session. */
  signature: string
  reload: () => void
}

export interface ReferenceHandoffOptions {
  progress?: ResumeProgressFacts
  seed?: ReferenceHandoffSeed | null
  vaults?: readonly KnowledgeVault[]
}

type SetHandoffState = Dispatch<SetStateAction<ReferenceHandoffState>>

const emptyState = (): ReferenceHandoffState => ({
  acknowledged: [],
  briefCopied: false,
  copied: false,
  error: null,
  loading: false,
  pending: null,
  prepared: null,
  references: [],
})

/**
 * Drops the prepared work that is on screen while keeping the loaded reference list.
 * `discardWork` only releases the owned payload in refs; without this the panel would go
 * on offering exports over a payload every handler already refuses.
 */
function withoutPreparedWork(current: ReferenceHandoffState): ReferenceHandoffState {
  return {
    ...current,
    acknowledged: [],
    briefCopied: false,
    copied: false,
    error: null,
    loading: false,
    pending: null,
    prepared: null,
  }
}

function failMessage(error: unknown) {
  if (error instanceof ReferenceHandoffError) return error.message
  return knowledgeErrorMessage(error)
}

async function liveTask(taskId: string) {
  const detail = await api.getTask(taskId)
  return {
    id: detail.task.id,
    uid: detail.task.uid,
    revision: detail.task.revision,
    title: detail.task.title,
    detail: detail.task.detail,
    status: detail.task.status,
  }
}

async function liveWorkspaceUid() {
  return (await api.getWorkspace()).workspace.id
}

async function readPinnedReference(
  binding: KnowledgeBinding,
  saved: KnowledgeSavedReference,
  signal: AbortSignal,
) {
  try {
    const data = await requestKnowledge('read-reference', {
      binding,
      document_path: saved.document_path,
      end_line: saved.end_line,
      expected_sha256: saved.source_sha256,
      start_line: saved.start_line,
      vault_id: saved.vault_id,
    }, STATUS_TIMEOUT_MS, signal)
    return data.reference
  } catch (error) {
    if (isKnowledgeCancelled(error)) throw error
    throw new ReferenceHandoffError(
      error instanceof KnowledgeHostError ? error.code : 'read_failed',
      `Could not read ${saved.document_path}. Nothing was exported.`,
    )
  }
}

async function listCurrentReferences(binding: KnowledgeBinding, signal: AbortSignal) {
  try {
    const data = await requestKnowledge('list-references', { binding }, STATUS_TIMEOUT_MS, signal)
    return data.references
  } catch (error) {
    if (isKnowledgeCancelled(error)) throw error
    throw new ReferenceHandoffError(
      error instanceof KnowledgeHostError ? error.code : 'list_failed',
      'Could not confirm the current linked references. Nothing was exported.',
    )
  }
}

function useHandoffFlight(setState: SetHandoffState) {
  const flight = useRef(0)
  const abortRef = useRef<AbortController | null>(null)
  const still = (token: number) => token === flight.current
  const stop = () => {
    abortRef.current?.abort()
    abortRef.current = null
    flight.current += 1
  }
  const begin = (pending: ReferenceHandoffState['pending']) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const token = ++flight.current
    setState((current) => ({
      ...current,
      briefCopied: false,
      copied: false,
      error: null,
      loading: pending === 'list',
      pending,
    }))
    return { signal: controller.signal, token }
  }
  return { abortRef, begin, still, stop }
}

type HandoffFlight = ReturnType<typeof useHandoffFlight>

function useHandoffOwner(sessionKey: string) {
  const nonce = useRef(0)
  const ownerRef = useRef('')
  const lastSession = useRef('')
  const preparedRef = useRef<PreparedReferenceHandoff | null>(null)
  const preparedOwnerRef = useRef('')
  if (lastSession.current !== sessionKey) {
    lastSession.current = sessionKey
    nonce.current += 1
    ownerRef.current = `${sessionKey}#${nonce.current}`
    preparedRef.current = null
    preparedOwnerRef.current = ''
  }
  const assign = () => {
    nonce.current += 1
    ownerRef.current = `${sessionKey}#${nonce.current}`
    return ownerRef.current
  }
  const forgetPrepared = () => {
    preparedRef.current = null
    preparedOwnerRef.current = ''
  }
  return { assign, forgetPrepared, ownerNow: () => ownerRef.current, preparedOwnerRef, preparedRef }
}

type HandoffOwner = ReturnType<typeof useHandoffOwner>

function loadReferenceList(
  binding: KnowledgeBinding,
  flight: HandoffFlight,
  referencesRef: MutableRefObject<KnowledgeSavedReference[]>,
  setState: SetHandoffState,
) {
  if (!knowledgeHostAvailable()) {
    setState({ ...emptyState(), error: HOST_UNAVAILABLE })
    return
  }
  const { signal, token } = flight.begin('list')
  void requestKnowledge('list-references', { binding }, STATUS_TIMEOUT_MS, signal).then(
    (listed) => {
      if (!flight.still(token)) return
      referencesRef.current = listed.references
      setState({ ...emptyState(), references: listed.references })
    },
    (error) => {
      if (!flight.still(token) || isKnowledgeCancelled(error)) return
      setState({ ...emptyState(), error: failMessage(error) })
    },
  )
}

interface PrepareInput {
  binding: KnowledgeBinding
  flight: HandoffFlight
  owner: HandoffOwner
  progress: ResumeProgressFacts
  references: readonly KnowledgeSavedReference[]
  selectedIds: readonly string[]
  setState: SetHandoffState
  vaults: readonly KnowledgeVault[]
}

function startPrepare(input: PrepareInput) {
  const { binding, flight, owner, setState } = input
  const { signal, token } = flight.begin('prepare')
  const tokenOwner = owner.assign()
  owner.forgetPrepared()
  void prepareReferenceHandoff({
    binding,
    listReferences: (listSignal) => listCurrentReferences(binding, listSignal),
    progress: input.progress,
    readLiveTask: liveTask,
    readReference: (saved, readSignal) => readPinnedReference(binding, saved, readSignal),
    readWorkspaceUid: liveWorkspaceUid,
    references: input.references,
    selectedIds: input.selectedIds,
    signal,
    vaults: input.vaults,
  }).then(
    (prepared) => {
      if (!flight.still(token) || owner.ownerNow() !== tokenOwner) return
      owner.preparedRef.current = prepared
      owner.preparedOwnerRef.current = tokenOwner
      setState((current) => ({
        ...current,
        acknowledged: [],
        briefCopied: false,
        copied: false,
        error: null,
        loading: false,
        pending: null,
        prepared,
      }))
    },
    (error) => {
      if (!flight.still(token) || isKnowledgeCancelled(error) || owner.ownerNow() !== tokenOwner) return
      flight.stop()
      owner.assign()
      owner.forgetPrepared()
      setState((current) => ({
        ...current,
        acknowledged: [],
        briefCopied: false,
        copied: false,
        error: failMessage(error),
        loading: false,
        pending: null,
        prepared: null,
      }))
    },
  )
}

function copyPreparedText(
  flight: HandoffFlight,
  owner: HandoffOwner,
  prepared: PreparedReferenceHandoff | null,
  guards: { acknowledgedNow: () => readonly string[]; progressNow: () => ResumeProgressFacts },
  setState: SetHandoffState,
  text: string | null,
  copiedKind: 'json' | 'brief',
) {
  const frozen = owner.ownerNow()
  const payload = owner.preparedRef.current
  if (!payload || payload !== prepared || !text) return
  if (owner.preparedOwnerRef.current !== frozen) return
  if (!allChangedAcknowledged(payload.changedIds, guards.acknowledgedNow())) return
  if (resumeProgressChanged(payload.progressKey, guards.progressNow())) return
  const { token } = flight.begin('copy')
  const stillCopying = () => flight.still(token)
    && owner.ownerNow() === frozen
    && owner.preparedRef.current === payload
    && owner.preparedOwnerRef.current === frozen
    && allChangedAcknowledged(payload.changedIds, guards.acknowledgedNow())
    && !resumeProgressChanged(payload.progressKey, guards.progressNow())
  void copyTextToClipboard(text, stillCopying).then(
    () => {
      if (!stillCopying()) return
      setState((current) => ({
        ...current,
        briefCopied: copiedKind === 'brief',
        copied: copiedKind === 'json',
        pending: null,
      }))
    },
    (error) => {
      if (!stillCopying()) return
      setState((current) => ({
        ...current,
        briefCopied: false,
        copied: false,
        error: getErrorMessage(error),
        pending: null,
      }))
    },
  )
}

function downloadPrepared(
  binding: KnowledgeBinding,
  owner: HandoffOwner,
  prepared: PreparedReferenceHandoff | null,
  acknowledged: readonly string[],
  progress: ResumeProgressFacts,
  setState: SetHandoffState,
) {
  const frozen = owner.ownerNow()
  const payload = owner.preparedRef.current
  if (!payload || payload !== prepared) return
  if (owner.preparedOwnerRef.current !== frozen) return
  if (!allChangedAcknowledged(payload.changedIds, acknowledged)) return
  if (resumeProgressChanged(payload.progressKey, progress)) return
  try {
    downloadKnowledgeContextJson(knowledgeContextFilename(binding), payload.json)
    setState((current) => ({ ...current, briefCopied: false, copied: false, error: null }))
  } catch (error) {
    if (owner.ownerNow() !== frozen) return
    setState((current) => ({ ...current, error: getErrorMessage(error) }))
  }
}

function exportAvailability(state: ReferenceHandoffState, progress: ResumeProgressFacts) {
  const stale = Boolean(state.prepared) && resumeProgressChanged(state.prepared!.progressKey, progress)
  const allowed = state.pending === null
    && !stale
    && allChangedAcknowledged(state.prepared?.changedIds ?? [], state.acknowledged)
  return {
    briefStale: stale,
    canExport: Boolean(state.prepared) && allowed,
    canExportBrief: Boolean(state.prepared?.briefMarkdown) && allowed,
  }
}

interface HandoffInputs {
  acknowledgedRef: MutableRefObject<readonly string[]>
  progressRef: MutableRefObject<ResumeProgressFacts>
  referencesRef: MutableRefObject<KnowledgeSavedReference[]>
  seedRef: MutableRefObject<ReferenceHandoffSeed | null>
  selectedRef: MutableRefObject<string[]>
  vaultsRef: MutableRefObject<readonly KnowledgeVault[]>
}

/**
 * Mirrors the current render's inputs into refs, so a click handler always acts on what
 * is on screen now rather than on the values captured when it was created.
 */
function useHandoffInputs(
  progress: ResumeProgressFacts,
  seed: ReferenceHandoffSeed | null,
  vaults: readonly KnowledgeVault[],
  selectedIds: string[],
  acknowledged: string[],
): HandoffInputs {
  const acknowledgedRef = useRef<readonly string[]>(acknowledged)
  const progressRef = useRef<ResumeProgressFacts>(progress)
  const referencesRef = useRef<KnowledgeSavedReference[]>([])
  const seedRef = useRef<ReferenceHandoffSeed | null>(seed)
  const selectedRef = useRef<string[]>(selectedIds)
  const vaultsRef = useRef<readonly KnowledgeVault[]>(vaults)
  useEffect(() => { selectedRef.current = selectedIds }, [selectedIds])
  useEffect(() => { acknowledgedRef.current = acknowledged }, [acknowledged])
  useEffect(() => { progressRef.current = progress })
  useEffect(() => { vaultsRef.current = vaults })
  useEffect(() => { seedRef.current = seed })
  return { acknowledgedRef, progressRef, referencesRef, seedRef, selectedRef, vaultsRef }
}

function handoffSessionKey(
  workspaceUid: string,
  task: KnowledgeTaskRef,
  open: boolean,
  seed: ReferenceHandoffSeed | null,
) {
  return [
    workspaceUid,
    task.uid,
    task.id,
    task.revision,
    open ? 'open' : 'closed',
    seed ? `seed#${seed.signature}` : 'self',
  ].join(':')
}

/** Starts a selection session bound to one Task revision and one reference list. */
function startSession(input: {
  binding: KnowledgeBinding
  flight: HandoffFlight
  inputs: HandoffInputs
  open: boolean
  owner: HandoffOwner
  setSelectedIds: Dispatch<SetStateAction<string[]>>
  setState: SetHandoffState
}) {
  const { flight, inputs, owner, setSelectedIds, setState } = input
  flight.stop()
  owner.forgetPrepared()
  inputs.referencesRef.current = []
  inputs.selectedRef.current = []
  setSelectedIds([])
  owner.assign()
  if (!input.open) {
    setState(emptyState())
    return
  }
  const seeded = inputs.seedRef.current
  if (seeded) {
    inputs.referencesRef.current = [...seeded.references]
    setState({ ...emptyState(), references: [...seeded.references] })
    return
  }
  loadReferenceList(input.binding, flight, inputs.referencesRef, setState)
}

function handoffActions(input: {
  binding: KnowledgeBinding
  flight: HandoffFlight
  inputs: HandoffInputs
  owner: HandoffOwner
  prepared: PreparedReferenceHandoff | null
  progress: ResumeProgressFacts
  setState: SetHandoffState
}) {
  const { binding, flight, inputs, owner, prepared, setState } = input
  const guards = {
    acknowledgedNow: () => inputs.acknowledgedRef.current,
    progressNow: () => inputs.progressRef.current,
  }
  return {
    copy: () => copyPreparedText(
      flight, owner, prepared, guards, setState, prepared?.json ?? null, 'json',
    ),
    copyBrief: () => copyPreparedText(
      flight, owner, prepared, guards, setState, prepared?.briefMarkdown ?? null, 'brief',
    ),
    download: () => downloadPrepared(
      binding, owner, prepared, guards.acknowledgedNow(), input.progress, setState,
    ),
    prepare: () => startPrepare({
      binding,
      flight,
      owner,
      progress: inputs.progressRef.current,
      references: inputs.referencesRef.current,
      selectedIds: inputs.selectedRef.current,
      setState,
      vaults: inputs.vaultsRef.current,
    }),
  }
}

export function useReferenceHandoff(
  workspaceUid: string,
  task: KnowledgeTaskRef,
  open: boolean,
  options: ReferenceHandoffOptions = {},
) {
  const progress = options.progress ?? UNBOUND_RESUME_PROGRESS
  const seed = options.seed ?? null
  const vaults = options.vaults ?? []
  const [state, setState] = useState(emptyState)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const inputs = useHandoffInputs(progress, seed, vaults, selectedIds, state.acknowledged)
  const binding = handoffBinding(workspaceUid, task)
  const sessionKey = handoffSessionKey(workspaceUid, task, open, seed)
  const flight = useHandoffFlight(setState)
  const owner = useHandoffOwner(sessionKey)

  useEffect(() => {
    startSession({ binding, flight, inputs, open, owner, setSelectedIds, setState })
    return () => { flight.abortRef.current?.abort() }
  }, [sessionKey])

  const discardWork = useCallback(() => {
    flight.stop()
    owner.assign()
    owner.forgetPrepared()
  }, [flight, owner])

  const toggle = useCallback((referenceId: string) => {
    discardWork()
    setSelectedIds((current) => nextHandoffSelection(current, referenceId))
    setState(withoutPreparedWork)
  }, [discardWork])

  const acknowledge = useCallback((referenceId: string, checked: boolean) => {
    setState((current) => ({
      ...current,
      acknowledged: checked
        ? [...new Set([...current.acknowledged, referenceId])]
        : current.acknowledged.filter((id) => id !== referenceId),
      copied: false,
      briefCopied: false,
    }))
  }, [])

  const cancel = useCallback(() => {
    discardWork()
    setState((current) => ({
      ...current,
      briefCopied: false,
      copied: false,
      loading: false,
      pending: null,
      prepared: null,
    }))
  }, [discardWork])

  const reload = useCallback(() => {
    discardWork()
    setSelectedIds([])
    inputs.selectedRef.current = []
    const seeded = inputs.seedRef.current
    if (seeded) {
      // This branch returns before any list reset, so the visible brief is cleared here.
      // The non-seeded path gets the same effect from `loadReferenceList`.
      setState(withoutPreparedWork)
      seeded.reload()
      return
    }
    loadReferenceList(binding, flight, inputs.referencesRef, setState)
  }, [binding, discardWork, flight, inputs])

  return {
    acknowledge,
    binding,
    ...exportAvailability(state, progress),
    cancel,
    ...handoffActions({
      binding, flight, inputs, owner, prepared: state.prepared, progress, setState,
    }),
    reload,
    selectedIds,
    state,
    toggle,
  }
}
