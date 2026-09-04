import { Icon } from './Icon'
import { EmptyState, Pill } from './Primitives'
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
import { contextTitle, externalContext } from '../utils/taskContext'

/**
 * Q4: the shared context timeline. Moved unchanged from the Task feature so the
 * Workspace Graph popover can render context without importing feature code.
 * It depends only on neutral components, config, domain and utils.
 */

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

function TaskContextBody({ item, sourceUrl }: { item: TaskContextItem; sourceUrl: string | null }) {
  const normalized = item.normalized
  return <>
    <h3>{contextTitle(item)}</h3>
    {normalized?.context ? <p>{normalized.context}</p> : item.text ? <p>{item.text}</p> : null}
    {normalized?.action_items?.length ? <ul>{normalized.action_items.map((action, actionIndex) => <li key={action.id ?? actionIndex}>{action.title}</li>)}</ul> : null}
    {sourceUrl ? <a href={sourceUrl} rel="noopener noreferrer" target="_blank">Open source <Icon name="arrowUpRight" size={13} /></a> : null}
  </>
}

function TaskContextEntry({ item, providerGates }: {
  item: TaskContextItem
  providerGates: MicrosoftProviderGates
}) {
  const external = externalContext(item)
  const providerState = contextProviderState(item, providerGates)
  return <article className="context-entry">
    <span className={`timeline-mark ${external ? 'timeline-mark--external' : ''}`}><Icon name={external ? 'inbox' : 'context'} size={14} /></span>
    <div>
      <TaskContextHeader external={external} item={item} replyUnavailable={providerState.replyUnavailable} />
      <TaskContextBody item={item} sourceUrl={providerState.sourceUrl} />
    </div>
  </article>
}

function contextKey(item: TaskContextItem, index: number) {
  if (item.ref) return `${item.ref.kind}:${item.ref.id}`
  return item.id ? `${externalContext(item) ? 'capture' : 'note'}:${item.id}` : `legacy:${index}`
}

export function TaskContextTimeline({
  context,
  providerGates,
}: {
  context: TaskDetail['context']
  providerGates: MicrosoftProviderGates
}) {
  return (
    <div className="timeline-list">
      {context.length ? context.map((item, index) => <TaskContextEntry item={item} key={contextKey(item, index)} providerGates={providerGates} />) : (
        <EmptyState icon="context" title="No context yet">Link a sanitized Inbox capture or add a Context card to preserve why this work matters.</EmptyState>
      )}
    </div>
  )
}
