import type { ReactNode } from 'react'

import { Pill } from '../../components/Primitives'
import { freshnessLabel, freshnessTone } from './knowledgeSession'
import {
  referenceOriginLabel,
  type KnowledgeExcerptView,
  type KnowledgeReferenceView,
} from './knowledgeSourceView'
import './ReferenceRows.css'

/**
 * Shared reference presentation. These components read a source's view model and never a
 * document path, line span or vault id, so a source that describes its excerpts as pages
 * and blocks renders through exactly the same rows.
 */

export function ReferenceIdentity({
  view,
}: {
  view: Pick<KnowledgeReferenceView, 'documentTitle' | 'documentLocation' | 'excerptLocation' | 'sourceName'>
}) {
  return (
    <span className="reference-row__identity">
      <strong className="reference-row__title">{view.documentTitle}</strong>
      <span className="reference-row__origin">{referenceOriginLabel(view)}</span>
    </span>
  )
}

export function ReferenceChoiceRow({
  actions,
  disabled,
  limitReached,
  onToggle,
  selected,
  view,
}: {
  actions?: ReactNode
  disabled: boolean
  limitReached: boolean
  onToggle: () => void
  selected: boolean
  view: KnowledgeReferenceView
}) {
  return (
    <li className="reference-row">
      <label className="reference-row__choice">
        <input
          aria-label={`Include ${view.documentTitle} from ${view.sourceName}`}
          checked={selected}
          disabled={disabled || (!selected && limitReached)}
          onChange={onToggle}
          type="checkbox"
        />
        <span className="reference-row__body">
          <ReferenceIdentity view={view} />
          <span className="reference-row__reason">
            <span className="reference-row__reason-label">Linked reason</span>{' '}
            <span className="reference-row__reason-text">{view.linkedReason}</span>
          </span>
        </span>
      </label>
      {actions ? <div className="reference-row__extra">{actions}</div> : null}
    </li>
  )
}

export function ReferenceSummaryRow({ view }: { view: KnowledgeReferenceView }) {
  return (
    <li className="reference-row reference-row--summary">
      <span className="reference-row__body">
        <ReferenceIdentity view={view} />
        <span className="reference-row__reason">
          <span className="reference-row__reason-label">Linked reason</span>{' '}
          <span className="reference-row__reason-text">{view.linkedReason}</span>
        </span>
      </span>
    </li>
  )
}

export function ReferenceExcerptCard({
  children,
  view,
}: {
  children?: ReactNode
  view: KnowledgeExcerptView
}) {
  return (
    <article className="reference-excerpt">
      <header>
        <strong className="reference-row__title">{view.documentTitle}</strong>
        <Pill tone="neutral">Read-only</Pill>
        <Pill tone={freshnessTone(view.freshness)}>{freshnessLabel(view.freshness)}</Pill>
      </header>
      <p className="reference-row__origin">{referenceOriginLabel(view)}</p>
      <small className="reference-row__origin">{view.trustNote}</small>
      <pre className="reference-excerpt__body">{view.excerpt}</pre>
      {view.excerptTruncated ? (
        <small className="reference-row__origin">Excerpt truncated to the permitted read window.</small>
      ) : null}
      {children}
    </article>
  )
}
