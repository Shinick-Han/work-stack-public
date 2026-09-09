import { Icon } from './Icon'
import { Button, EmptyState, Pill } from './Primitives'
import {
  MICROSOFT_PROVIDERS,
  type MicrosoftProvider,
  type TaskDetail,
} from '../domain/types'
import {
  providerReplyVerified,
  type MicrosoftProviderGates,
} from '../config/providerGates'
import { formatDateTime, safeExternalUrl } from '../utils/format'
import {
  captureLinkRemovalTarget,
  contextPlainBody,
  contextTitle,
  contextUnknownFields,
  externalContext,
  type CaptureLinkRemovalTarget,
} from '../utils/taskContext'

/**
 * Q4: the shared context timeline. Moved unchanged from the Task feature so the
 * Workspace Graph popover can render context without importing feature code.
 * It depends only on neutral components, config, domain and utils.
 *
 * R22 adds one OPTIONAL removal callback and nothing else. This file owns no request,
 * no key, no cache and no error taxonomy: it renders the control, and the feature that
 * passes the callback owns every side effect. A caller that passes nothing — the
 * Workspace Graph popover — is byte-for-byte the read-only timeline it was.
 */

/**
 * The presentation contract for `Remove task link`.
 *
 * `pending` and `failure` name the card they belong to by capture id AND by the exact
 * revision the attempt was made at, so a refreshed card never inherits the previous
 * revision's error, and a stale answer cannot decorate a card the reader has since
 * seen change.
 */
export interface TaskContextLinkRemovalState {
  /** The Task whose explicit Capture links may be removed from this timeline. */
  taskId: string
  pending: { captureId: string; revision: number; retry: boolean } | null
  /** Closed copy only. `retry` marks the unknown outcome that may be retried as-is. */
  failure: { captureId: string; revision: number; message: string; retry: boolean } | null
  /**
   * R33: feedback for the ONE most recent removal in this view, rendered outside every
   * card. Null when the feature has nothing to offer, which is every legacy response.
   */
  undo: TaskContextUndoState | null
  /**
   * True while any removal or Undo request is in flight, or while an Undo outcome is
   * unknown: no card may start another write until that settles or is dismissed.
   */
  locked: boolean
  onRemove: (target: CaptureLinkRemovalTarget) => void
  onUndo: () => void
  /** Hides settled feedback locally. It sends nothing and restores nothing. */
  onDismissUndo: () => void
}

/**
 * R33: the presentation contract for `Undo link removal`.
 *
 * It deliberately carries no receipt, revision or key. Those belong to the feature that
 * owns the request; this file renders a phase and, when there is one, a closed sentence.
 */
export interface TaskContextUndoState {
  /** The Capture the removal was made against, for the feature's own bookkeeping. */
  captureId: string
  phase: 'offer' | 'pending' | 'restored' | 'failed' | 'unknown'
  /** Closed copy only, and only for a settled failure or an unknown outcome. */
  message: string | null
}

type TaskContextItem = TaskDetail['context'][number]

function contextProviderState(item: TaskContextItem, providerGates: MicrosoftProviderGates) {
  const source = item.source
  const microsoftProvider = source && MICROSOFT_PROVIDERS.includes(source.provider as MicrosoftProvider)
    ? source.provider as MicrosoftProvider
    : null
  return {
    replyUnavailable: microsoftProvider !== null && !providerReplyVerified(microsoftProvider, providerGates),
    source,
    sourceUrl: safeExternalUrl(source?.web_url),
  }
}

function TaskContextHeader({ external, item, replyUnavailable }: { external: boolean; item: TaskContextItem; replyUnavailable: boolean }) {
  const created = item.created_at ?? item.created
  const dateLabel = item.date_precision === 'date'
    ? created
    : item.date_precision === 'unknown' ? 'Unknown time' : formatDateTime(created)
  return <header>
    <Pill tone={external ? 'verified' : 'neutral'}>{external ? item.source?.provider ?? 'External context' : 'Context card'}</Pill>
    {replyUnavailable ? <Pill tone="neutral">Reply unavailable · Gate 0 pending</Pill> : null}
    <time dateTime={item.date_precision === 'unknown' ? undefined : created}>{dateLabel}</time>
  </header>
}

function TaskContextUnknownFields({ item }: { item: TaskContextItem }) {
  const extras = contextUnknownFields(item)
  if (extras.length === 0) return null
  return (
    <details className="context-entry__extras">
      <summary>{`Additional recorded data (${extras.length})`}</summary>
      <dl>
        {extras.map((extra) => (
          <div key={extra.label}>
            <dt>{extra.label}</dt>
            <dd>{extra.value}</dd>
          </div>
        ))}
      </dl>
    </details>
  )
}

function TaskContextBody({ item, sourceUrl }: { item: TaskContextItem; sourceUrl: string | null }) {
  const normalized = item.normalized
  const body = contextPlainBody(item)
  return <>
    <h3>{contextTitle(item)}</h3>
    {body ? <p>{body}</p> : null}
    {normalized?.action_items?.length ? <ul>{normalized.action_items.map((action, actionIndex) => <li key={action.id ?? actionIndex}>{action.title}</li>)}</ul> : null}
    {sourceUrl ? <a href={sourceUrl} rel="noopener noreferrer" target="_blank">Open source <Icon name="arrowUpRight" size={13} /></a> : null}
    <TaskContextUnknownFields item={item} />
  </>
}

/**
 * The action, when the feature asked for one and this exact card is the Task's own
 * explicit Capture link.
 *
 * Nothing is optimistic. The card is still on screen while the request is in flight and
 * after an unknown outcome; only the feature's refreshed data removes it. An unknown
 * outcome offers `Try again`, which the feature answers with the SAME request under the
 * same key; a determinate refusal offers no control at all, because retrying the same
 * stale revision would be refused for the same reason.
 */
function TaskContextLinkAction({ item, removal }: {
  item: TaskContextItem
  removal?: TaskContextLinkRemovalState
}) {
  const target = removal ? captureLinkRemovalTarget(item, removal.taskId) : null
  if (!removal || !target) return null
  const mine = (entry: { captureId: string; revision: number }) => (
    entry.captureId === target.captureId && entry.revision === target.revision
  )
  const pending = removal.pending !== null && mine(removal.pending) ? removal.pending : null
  const failure = removal.failure !== null && mine(removal.failure) ? removal.failure : null
  // R33 widens the existing lock: an Undo in flight, or one whose outcome nobody knows,
  // blocks every card exactly as an in-flight removal already did.
  const locked = removal.locked
  return <div className="context-entry__actions">
    {failure && !failure.retry ? null : (
      <Button
        aria-busy={pending !== null}
        disabled={locked}
        onClick={() => removal.onRemove(target)}
      >{failure?.retry ? 'Try again' : 'Remove task link'}</Button>
    )}
    {pending ? <p role="status">{pending.retry
      ? 'Checking whether the earlier request removed the task link…'
      : 'Removing the task link…'}</p> : null}
    {failure ? <p className="context-entry__action-error" role="alert">{failure.message}</p> : null}
  </div>
}

function TaskContextEntry({ item, providerGates, removal }: {
  item: TaskContextItem
  providerGates: MicrosoftProviderGates
  removal?: TaskContextLinkRemovalState
}) {
  const external = externalContext(item)
  const providerState = contextProviderState(item, providerGates)
  return <article className="context-entry">
    <span className={`timeline-mark ${external ? 'timeline-mark--external' : ''}`}><Icon name={external ? 'inbox' : 'context'} size={14} /></span>
    <div>
      <TaskContextHeader external={external} item={item} replyUnavailable={providerState.replyUnavailable} />
      <TaskContextBody item={item} sourceUrl={providerState.sourceUrl} />
      <TaskContextLinkAction item={item} removal={removal} />
    </div>
  </article>
}

/** The one live sentence per Undo phase. `null` phases carry a closed failure message. */
const UNDO_PHASE_COPY: Readonly<Record<string, string>> = Object.freeze({
  offer: 'The task link was removed.',
  pending: 'Restoring the task link…',
  restored: 'The task link was restored.',
})

/**
 * R33: removal feedback for this VIEW, deliberately outside every card.
 *
 * Removing the last explicit link empties the timeline, and a control living inside the
 * card would leave with it — so the reader would lose the Undo at exactly the moment it
 * became useful. This banner is a sibling of the list and renders over the empty state
 * too.
 *
 * Nothing here is optimistic and nothing here is automatic. `Undo link removal` and its
 * same-intent retry are the only controls that reach the feature's request; `Dismiss`
 * hides local feedback and claims nothing. A surface that passes no callback — the
 * Workspace Graph popover — renders none of it.
 */
function TaskContextUndoBanner({ removal }: { removal?: TaskContextLinkRemovalState }) {
  const undo = removal?.undo
  if (!removal || !undo) return null
  const pending = undo.phase === 'pending'
  const retry = undo.phase === 'unknown'
  const live = Object.hasOwn(UNDO_PHASE_COPY, undo.phase) ? UNDO_PHASE_COPY[undo.phase] : null
  return <div className="context-entry__actions" role="group" aria-label="Removed task link">
    {live ? <p role="status">{live}</p> : null}
    {undo.message ? <p className="context-entry__action-error" role="alert">{undo.message}</p> : null}
    {undo.phase === 'offer' || retry ? (
      <Button
        aria-busy={false}
        disabled={removal.pending !== null}
        onClick={removal.onUndo}
      >{retry ? 'Try Undo again' : 'Undo link removal'}</Button>
    ) : null}
    {/* A pending Undo has nothing settled to dismiss, so the control simply is not there. */}
    {pending ? null : <Button onClick={removal.onDismissUndo}>Dismiss</Button>}
  </div>
}

function contextKey(item: TaskContextItem, index: number) {
  if (item.ref) return `${item.ref.kind}:${item.ref.id}`
  return item.id ? `${externalContext(item) ? 'capture' : 'note'}:${item.id}` : `legacy:${index}`
}

export function TaskContextTimeline({
  context,
  providerGates,
  removal,
}: {
  context: TaskDetail['context']
  providerGates: MicrosoftProviderGates
  /** Omitted by every read-only surface, including the Workspace Graph popover. */
  removal?: TaskContextLinkRemovalState
}) {
  return (
    <div className="timeline-list">
      <TaskContextUndoBanner removal={removal} />
      {context.length ? context.map((item, index) => <TaskContextEntry item={item} key={contextKey(item, index)} providerGates={providerGates} removal={removal} />) : (
        <EmptyState icon="context" title="No context yet">Link a sanitized Inbox capture or add a Context card to preserve why this work matters.</EmptyState>
      )}
    </div>
  )
}
