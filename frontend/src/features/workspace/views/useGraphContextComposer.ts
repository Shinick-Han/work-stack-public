import {
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type FormEvent,
  type Ref,
} from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, CommitUnknownError, createIdempotencyKey } from '../../../api/client'
import { getErrorMessage } from '../../../utils/format'
import {
  canApplyComposerResult,
  composerCloseKind,
  composerSubmitDisabled,
  composerSubmitRequest,
  type ComposerCloseKind,
  type FrozenGraphContextRequest,
  type GraphContextComposerOwner,
} from './GraphContextCardComposerModel'

export type GraphContextCardComposerHandle = {
  requestDismiss: (proceed: () => void) => void
}

/** An open close-guard: why closing is held, and what closing would do. */
export type GraphContextCloseGuard = {
  kind: Exclude<ComposerCloseKind, 'open'>
  proceed: () => void
}

type ComposerOutcome =
  | { ok: true }
  | { ok: false; unknown: boolean; message: string }

function invalidateCreatedCard(queryClient: ReturnType<typeof useQueryClient>, taskId: string) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ['task', taskId] }),
    queryClient.invalidateQueries({ queryKey: ['workspace'] }),
  ])
}

/** Sends one frozen request and reports how it settled; never rejects. */
async function commitContextCard(
  payload: FrozenGraphContextRequest,
  invalidate: () => Promise<unknown>,
): Promise<ComposerOutcome> {
  try {
    await api.createNote(payload.text, [payload.taskId], payload.key)
  } catch (caught) {
    if (caught instanceof CommitUnknownError) {
      return { ok: false, unknown: true, message: caught.message }
    }
    return { ok: false, unknown: false, message: getErrorMessage(caught) }
  }
  try {
    await invalidate()
  } catch {
    // The card itself committed; a stale query is not a second write.
  }
  return { ok: true }
}

/**
 * Holds a close that would lose work, and answers the owner's dismiss request
 * from a live snapshot rather than from a rendered value.
 */
function useComposerCloseGuard(
  ref: Ref<GraphContextCardComposerHandle> | undefined,
  snapshot: () => { text: string; pending: boolean; unknown: boolean },
) {
  const [guard, setGuard] = useState<GraphContextCloseGuard | null>(null)
  const stayRef = useRef<HTMLButtonElement>(null)
  const guardRef = useRef(guard)
  const snapshotRef = useRef(snapshot)
  guardRef.current = guard
  snapshotRef.current = snapshot

  useEffect(() => {
    if (guard) stayRef.current?.focus()
  }, [guard])

  useImperativeHandle(ref, () => ({
    requestDismiss(proceed) {
      if (guardRef.current) {
        setGuard(null)
        return
      }
      const kind = composerCloseKind(snapshotRef.current())
      if (kind === 'open') {
        proceed()
        return
      }
      setGuard({ kind, proceed })
    },
  }))

  return { guard, setGuard, stayRef }
}

/**
 * Which attempt still owns this composer. A response may only be applied while
 * its token is the live one and the same Task and workspace are still mounted.
 */
function useComposerAttempt(
  owner: GraphContextComposerOwner,
  ownerAliveRef?: { current: boolean },
) {
  const tokenRef = useRef<object>({})
  const pendingTokenRef = useRef<object | null>(null)
  const ownerRef = useRef<GraphContextComposerOwner>(owner)
  ownerRef.current = owner

  useEffect(() => {
    return () => { tokenRef.current = {} }
  }, [])

  const isCurrent = (token: object, submitted: GraphContextComposerOwner) => canApplyComposerResult({
    ownerAlive: ownerAliveRef ? ownerAliveRef.current : true,
    submittedToken: token,
    liveToken: tokenRef.current,
    submitted,
    live: ownerRef.current,
  })

  return { tokenRef, pendingTokenRef, isCurrent }
}

type UseGraphContextComposerArgs = {
  taskId: string
  workspaceId: string
  ownerAliveRef?: { current: boolean }
  ref?: Ref<GraphContextCardComposerHandle>
}

/**
 * The composer's whole lifecycle: one in-flight request at a time, an
 * unverified commit frozen to its exact payload, and a late response that is
 * applied only while the same Task and workspace still own this composer.
 */
export function useGraphContextComposer({
  taskId,
  workspaceId,
  ownerAliveRef,
  ref,
}: UseGraphContextComposerArgs) {
  const queryClient = useQueryClient()
  const [text, setText] = useState('')
  const [pending, setPending] = useState(false)
  const [unknown, setUnknown] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const keyRef = useRef<string | null>(null)
  const frozenRef = useRef<FrozenGraphContextRequest | null>(null)
  const textRef = useRef(text)
  const unknownRef = useRef(unknown)
  textRef.current = text
  unknownRef.current = unknown
  const { tokenRef, pendingTokenRef, isCurrent } = useComposerAttempt(
    { taskId, workspaceId },
    ownerAliveRef,
  )
  const { guard, setGuard, stayRef } = useComposerCloseGuard(ref, () => ({
    text: textRef.current,
    pending: pendingTokenRef.current !== null,
    unknown: unknownRef.current,
  }))

  const applyOutcome = (outcome: ComposerOutcome, payload: FrozenGraphContextRequest) => {
    if (outcome.ok) {
      keyRef.current = null
      frozenRef.current = null
      setUnknown(false)
      setError(null)
      setText('')
      setGuard(null)
      return
    }
    frozenRef.current = outcome.unknown ? payload : null
    setUnknown(outcome.unknown)
    setError(outcome.message)
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (pendingTokenRef.current) return
    const payload = composerSubmitRequest({
      unknown,
      frozen: frozenRef.current,
      draft: { text, taskId, workspaceId, key: keyRef.current ?? createIdempotencyKey() },
    })
    if (!payload) return
    const token = tokenRef.current
    const owner: GraphContextComposerOwner = { taskId: payload.taskId, workspaceId: payload.workspaceId }
    keyRef.current = payload.key
    if (unknown) frozenRef.current = payload
    pendingTokenRef.current = token
    setPending(true)
    setError(null)
    setText(payload.text)
    void commitContextCard(payload, () => invalidateCreatedCard(queryClient, payload.taskId)).then((outcome) => {
      if (isCurrent(token, owner)) applyOutcome(outcome, payload)
      if (pendingTokenRef.current === token) pendingTokenRef.current = null
      if (isCurrent(token, owner)) setPending(false)
    })
  }

  const editDraft = (value: string) => {
    if (pending || unknown) return
    keyRef.current = null
    setText(value)
  }

  const discardDraft = (proceed: () => void) => {
    setGuard(null)
    setText('')
    keyRef.current = null
    proceed()
  }

  return {
    text,
    pending,
    unknown,
    error,
    guard,
    stayRef,
    submit,
    editDraft,
    discardDraft,
    keepDraft: () => setGuard(null),
    submitDisabled: composerSubmitDisabled({ pending, unknown, text }),
  }
}
