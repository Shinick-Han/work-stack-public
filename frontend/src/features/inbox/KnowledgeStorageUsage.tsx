import type { KnowledgeOccupancy } from '../../api/knowledge'
import './KnowledgeStorageUsage.css'

/**
 * What the owner's request store held at the last policy read or save.
 *
 * Presentational and nothing else. It reads no state, starts no request, keeps no timer
 * and offers no control: the numbers are handed to it, and the moment they were observed
 * is part of what it says. That is the whole honesty of this region — a request issued
 * after the observation does not update these counts, so they are never labelled as the
 * current capacity, only as the last one an explicit read or save returned.
 *
 * It also refuses to guess. There is no "n searches left": the byte bound and the count
 * bound are separate, either can be the one that stops the next request, and the size of
 * a request the user has not written yet is unknown. Nothing here proposes a deletion or
 * a cleanup either. Reaching a bound is reported; what to do about it stays the owner's,
 * and this snapshot never blocks a request, an import, a replay or a policy save.
 */

export const STORAGE_USAGE_TITLE = 'Search request storage'
export const STORAGE_USAGE_UNAVAILABLE = 'Storage usage unavailable'
export const STORAGE_USAGE_OBSERVED = 'At last policy read/save'
export const STORAGE_USAGE_OUTDATED =
  'Out of date: this is from an earlier read or save, not what the server holds now.'
export const STORAGE_USAGE_AT_BOUND = 'Storage limit reached at last read/save'
const STORAGE_USAGE_NOTE =
  'Stored requests include completed and expired requests. Byte usage can stop a new request before the request count does.'

/** Exact digits, grouped for reading. No rounding to KB: the bound is an exact byte count. */
function digits(value: number): string {
  return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

function percent(value: number, bound: number): number {
  return Math.min(100, Math.max(0, Math.round((value / bound) * 100)))
}

function UsageBar({ bound, label, text, value }: {
  bound: number
  label: string
  text: string
  value: number
}) {
  return (
    <div className="knowledge-usage__meter">
      <div className="knowledge-usage__row">
        <span className="knowledge-usage__label">{label}</span>
        <span className="knowledge-usage__value">{text}</span>
      </div>
      <div
        aria-label={label}
        aria-valuemax={bound}
        aria-valuemin={0}
        aria-valuenow={value}
        aria-valuetext={text}
        className="knowledge-usage__bar"
        role="progressbar"
      >
        <span style={{ width: `${percent(value, bound)}%` }} />
      </div>
    </div>
  )
}

export interface KnowledgeStorageUsageProps {
  /** The last validated observation, or `null` when there is not one to show. */
  usage: KnowledgeOccupancy | null
  /**
   * The policy behind this observation is being re-read or was not confirmed by a later
   * read, so the numbers are retained but explicitly marked as no longer current.
   */
  outdated: boolean
}

export function KnowledgeStorageUsage({ outdated, usage }: KnowledgeStorageUsageProps) {
  const atBound =
    usage !== null &&
    (usage.request_count >= usage.request_bound || usage.encoded_bytes >= usage.byte_bound)
  return (
    <section aria-label={STORAGE_USAGE_TITLE} className="knowledge-usage">
      <h4 className="knowledge-usage__title">{STORAGE_USAGE_TITLE}</h4>
      {usage === null ? (
        <p className="knowledge-usage__note">{STORAGE_USAGE_UNAVAILABLE}</p>
      ) : (
        <>
          <UsageBar
            bound={usage.request_bound}
            label="Stored requests"
            text={`${digits(usage.request_count)} of ${digits(usage.request_bound)} requests`}
            value={usage.request_count}
          />
          <UsageBar
            bound={usage.byte_bound}
            label="Stored size"
            text={`${digits(usage.encoded_bytes)} of ${digits(usage.byte_bound)} bytes`}
            value={usage.encoded_bytes}
          />
          <p className="knowledge-usage__observed">
            {outdated ? STORAGE_USAGE_OUTDATED : STORAGE_USAGE_OBSERVED}
          </p>
          {atBound ? (
            <p className="knowledge-usage__bound" role="status">{STORAGE_USAGE_AT_BOUND}</p>
          ) : null}
          <p className="knowledge-usage__note">{STORAGE_USAGE_NOTE}</p>
        </>
      )}
    </section>
  )
}
