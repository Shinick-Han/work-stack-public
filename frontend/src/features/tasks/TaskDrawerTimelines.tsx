import { Icon } from '../../components/Icon'
import { EmptyState } from '../../components/Primitives'
import type { TaskDetail } from '../../domain/types'
import { formatDateTime } from '../../utils/format'
import { activityTitle } from './taskDrawerModel'

/**
 * Q4: the context timeline moved to components/TaskContextTimeline. It is
 * re-exported here so TaskDrawer's existing import stays valid and no consumer
 * or test has to change.
 */
export { TaskContextTimeline } from '../../components/TaskContextTimeline'

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
