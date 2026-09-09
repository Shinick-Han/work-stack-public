import { useEffect, useRef, useState } from 'react'
import { Button } from '../../components/Primitives'
import type { IssuedKnowledgeRequestWire } from '../../api/knowledge'
import { describeKnowledgeExecutionFailure, executeKnowledgeRequest } from '../../api/knowledgeExecution'
import { isKnowledgeRequestExpired, type IssuedKnowledgeRequest } from './knowledgeRequestDraft'
import { acceptKnowledgeExecutionProposal } from './knowledgeExecutionAccept'
import type { KnowledgeImportEnvelope } from './knowledgeCaptureImport'
import './KnowledgeExecutionPanel.css'

const RUN_LABEL = 'Run connected search'
const REVIEW_LABEL = 'Review results'
const PENDING_NOTE = 'Waiting for the configured connector…'
const EXPIRED_BEFORE =
  'This request has expired. It is not run automatically. If you already have a result, paste it into Import.'
const EXPIRED_DURING =
  'This request is no longer current. The result is not offered for review. If you already have a result, paste it into Import.'
const LEDE =
  'Run the connector the operator configured for this server. Nothing is imported until you review the result. The default server has no connector until an operator configures one.'
const MANUAL_NOTE =
  'You can still copy this request and paste an existing result into Import. Closing this window does not cancel work already sent.'

export interface KnowledgeExecutionPanelProps {
  expired: boolean
  now: () => number
  onReview?: (envelope: KnowledgeImportEnvelope) => void
  request: IssuedKnowledgeRequest
  workspaceUid: string
}

type Phase = 'idle' | 'pending' | 'ready' | 'failed'

/**
 * The panel's whole surface. It decides nothing: every affordance below is a
 * prop the mounted panel already resolved, so no guard lives in two places.
 */
function KnowledgeExecutionView({
  canReview,
  canRun,
  error,
  onReview,
  onRun,
  pending,
}: {
  canReview: boolean
  canRun: boolean
  error: string | null
  onReview: () => void
  onRun: () => void
  pending: boolean
}) {
  return (
    <section aria-label="Connected search" className="knowledge-execution">
      <p className="knowledge-execution__lede">{LEDE}</p>
      <div className="knowledge-execution__actions">
        {canRun ? (
          <Button icon="search" onClick={onRun} type="button" variant="primary">
            {RUN_LABEL}
          </Button>
        ) : null}
        {pending ? (
          <Button disabled icon="search" type="button" variant="primary">
            Running…
          </Button>
        ) : null}
        {canReview ? (
          <Button icon="inbox" onClick={onReview} type="button" variant="primary">
            {REVIEW_LABEL}
          </Button>
        ) : null}
      </div>
      {pending ? <p className="knowledge-execution__pending" role="status">{PENDING_NOTE}</p> : null}
      {error ? <p className="inline-error" role="alert">{error}</p> : null}
      <p className="knowledge-execution__footnote">{MANUAL_NOTE}</p>
    </section>
  )
}

/**
 * One mounted execution for one issued receipt. The parent keys this by receipt
 * identity so a close, workspace change or new request discards in-flight results.
 * Clicks own the POST: nothing here runs from an effect.
 */
export function KnowledgeExecutionPanel({
  expired,
  now,
  onReview,
  request,
  workspaceUid,
}: KnowledgeExecutionPanelProps) {
  const attemptedRef = useRef(false)
  const generationRef = useRef(0)
  const mountedRef = useRef(true)
  const workspaceRef = useRef(workspaceUid)
  const requestIdRef = useRef(request.request_id)
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState<string | null>(null)
  const [envelope, setEnvelope] = useState<KnowledgeImportEnvelope | null>(null)

  workspaceRef.current = workspaceUid
  requestIdRef.current = request.request_id

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const current = () =>
    mountedRef.current &&
    workspaceRef.current === workspaceUid &&
    requestIdRef.current === request.request_id

  const run = () => {
    if (attemptedRef.current) return
    if (expired || isKnowledgeRequestExpired(request, now())) {
      setError(EXPIRED_BEFORE)
      setPhase('failed')
      return
    }
    attemptedRef.current = true
    const token = ++generationRef.current
    const expectedWorkspace = workspaceUid
    const expectedRequestId = request.request_id
    setPhase('pending')
    setError(null)
    setEnvelope(null)
    void executeKnowledgeRequest(request as IssuedKnowledgeRequestWire).then(
      (data) => {
        if (!mountedRef.current || generationRef.current !== token) return
        if (workspaceRef.current !== expectedWorkspace || requestIdRef.current !== expectedRequestId) return
        if (expired || isKnowledgeRequestExpired(request, now())) {
          setPhase('failed')
          setError(EXPIRED_DURING)
          return
        }
        try {
          const accepted = acceptKnowledgeExecutionProposal(data, request)
          if (!current() || generationRef.current !== token) return
          if (isKnowledgeRequestExpired(request, now())) {
            setPhase('failed')
            setError(EXPIRED_DURING)
            return
          }
          setEnvelope(accepted)
          setPhase('ready')
        } catch (caught) {
          if (!mountedRef.current || generationRef.current !== token) return
          setPhase('failed')
          setError(describeKnowledgeExecutionFailure(caught))
        }
      },
      (caught: unknown) => {
        if (!mountedRef.current || generationRef.current !== token) return
        if (workspaceRef.current !== expectedWorkspace || requestIdRef.current !== expectedRequestId) return
        setPhase('failed')
        setError(describeKnowledgeExecutionFailure(caught))
      },
    )
  }

  const review = () => {
    if (!envelope || !onReview) return
    if (expired || isKnowledgeRequestExpired(request, now())) {
      setEnvelope(null)
      setPhase('failed')
      setError(EXPIRED_DURING)
      return
    }
    onReview(envelope)
  }

  const canRun = phase === 'idle' && !expired
  const canReview = phase === 'ready' && envelope !== null && !expired

  return (
    <KnowledgeExecutionView canReview={canReview} canRun={canRun} error={error} onReview={review} onRun={run} pending={phase === 'pending'} />
  )
}
