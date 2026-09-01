import { Icon } from '../../components/Icon'
import { EmptyState, Pill } from '../../components/Primitives'
import {
  MICROSOFT_PROVIDERS,
  type MicrosoftProvider,
  type TaskDetail,
} from '../../domain/types'
import {
  providerReplyVerified,
  type MicrosoftProviderGates,
} from '../../config/providerGates'
import { formatDateTime, safeExternalUrl } from '../../utils/format'
import { activityTitle, contextTitle, externalContext } from './taskDrawerModel'

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
  return <header>
    <Pill tone={external ? 'verified' : 'neutral'}>{external ? item.source?.provider ?? 'External context' : 'Context card'}</Pill>
    {replyUnavailable ? <Pill tone="neutral">Reply unavailable · Gate 0 pending</Pill> : null}
    <time>{formatDateTime(item.created_at ?? item.created)}</time>
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

function TaskContextEntry({ index, item, providerGates }: {
  index: number
  item: TaskContextItem
  providerGates: MicrosoftProviderGates
}) {
  const external = externalContext(item)
  const providerState = contextProviderState(item, providerGates)
  return <article className="context-entry" key={item.id ?? index}>
    <span className={`timeline-mark ${external ? 'timeline-mark--external' : ''}`}><Icon name={external ? 'inbox' : 'context'} size={14} /></span>
    <div>
      <TaskContextHeader external={external} item={item} replyUnavailable={providerState.replyUnavailable} />
      <TaskContextBody item={item} sourceUrl={providerState.sourceUrl} />
    </div>
  </article>
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
      {context.length ? context.map((item, index) => <TaskContextEntry index={index} item={item} key={item.id ?? index} providerGates={providerGates} />) : (
        <EmptyState icon="context" title="No context yet">Link a sanitized Inbox capture or add a Context card to preserve why this work matters.</EmptyState>
      )}
    </div>
  )
}

export function TaskActivityTimeline({ activity }: { activity: TaskDetail['activity'] }) {
  return (
    <div className="timeline-list">
      {activity.length ? activity.map((item, index) => (
        <article className="activity-entry" key={item.id ?? index}>
          <span className="timeline-mark"><Icon name="activity" size={14} /></span>
          <div>
            <h3>{activityTitle(item)}</h3>
            <time>{formatDateTime(item.created_at ?? item.at)}</time>
            {item.type === 'task.planning_status' && item.prior_revision !== null && item.prior_revision !== undefined
              ? <p>Revision {item.prior_revision} → {item.new_revision}</p>
              : null}
            {item.actor ? <p>By {item.actor}{item.provenance ? ` · ${item.provenance}` : ''}</p> : null}
          </div>
        </article>
      )) : (
        <EmptyState icon="activity" title="No activity yet">Changes to status and linked context will appear here.</EmptyState>
      )}
    </div>
  )
}
