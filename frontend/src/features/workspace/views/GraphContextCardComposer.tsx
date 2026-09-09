import { type Ref, type RefObject } from 'react'
import {
  GRAPH_CONTEXT_COMPOSER_COPY as COPY,
  composerSubmitLabel,
} from './GraphContextCardComposerModel'
import {
  useGraphContextComposer,
  type GraphContextCardComposerHandle,
  type GraphContextCloseGuard,
} from './useGraphContextComposer'

export type { GraphContextCardComposerHandle } from './useGraphContextComposer'

interface GraphContextCardComposerProps {
  taskId: string
  workspaceId: string
  ownerAliveRef?: { current: boolean }
  ref?: Ref<GraphContextCardComposerHandle>
}

export function GraphContextCardComposer({
  taskId,
  workspaceId,
  ownerAliveRef,
  ref,
}: GraphContextCardComposerProps) {
  const composer = useGraphContextComposer({ taskId, workspaceId, ownerAliveRef, ref })
  const { text, pending, unknown, error, guard } = composer

  return (
    <>
      <form className="wsv-graph-context__composer" onSubmit={composer.submit} aria-busy={pending || undefined}>
        <div>
          <h3>{COPY.heading}</h3>
          <p>{COPY.linkedHint}</p>
        </div>
        <label>
          <span>{COPY.field}</span>
          <textarea
            name="context-card"
            rows={3}
            value={text}
            readOnly={unknown}
            disabled={pending}
            placeholder={COPY.placeholder}
            autoComplete="off"
            spellCheck
            onChange={(event) => composer.editDraft(event.target.value)}
          />
        </label>
        {error ? <p role="alert">{error}</p> : null}
        <button type="submit" disabled={composer.submitDisabled}>
          {composerSubmitLabel({ pending, unknown })}
        </button>
      </form>
      {guard ? (
        <GraphContextCloseGuardDialog
          guard={guard}
          stayRef={composer.stayRef}
          onKeep={composer.keepDraft}
          onDiscard={composer.discardDraft}
        />
      ) : null}
    </>
  )
}

/** Explicit choice before a close that would lose a draft or an in-flight save. */
function GraphContextCloseGuardDialog({
  guard,
  stayRef,
  onKeep,
  onDiscard,
}: {
  guard: GraphContextCloseGuard
  stayRef: RefObject<HTMLButtonElement | null>
  onKeep: () => void
  onDiscard: (proceed: () => void) => void
}) {
  return (
    <div
      className="wsv-graph-context__guard"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="graph-context-card-guard-title"
      aria-describedby="graph-context-card-guard-body"
    >
      <div className="wsv-graph-context__guard-card">
        <h3 id="graph-context-card-guard-title">
          {guard.kind === 'pending' ? COPY.closePendingTitle
            : guard.kind === 'unknown' ? COPY.closeUnknownTitle
              : COPY.closeDirtyTitle}
        </h3>
        <p id="graph-context-card-guard-body">
          {guard.kind === 'pending' ? COPY.closePendingBody
            : guard.kind === 'unknown' ? COPY.closeUnknownBody
              : COPY.closeDirtyBody}
        </p>
        <div className="wsv-graph-context__guard-actions">
          <button ref={stayRef} type="button" onClick={onKeep}>
            {guard.kind === 'dirty' ? COPY.keep : COPY.stay}
          </button>
          {guard.kind === 'dirty' ? (
            <button type="button" onClick={() => onDiscard(guard.proceed)}>
              {COPY.discard}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}
