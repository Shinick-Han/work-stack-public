import { render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'

import type { KnowledgeExcerptView, KnowledgeReferenceView } from './knowledgeSourceView'
import { ReferenceChoiceRow, ReferenceExcerptCard, ReferenceSummaryRow } from './ReferenceRows'

/**
 * A synthetic page/block source. It proves the shared rows need no file path, line span
 * or vault field. It is a presentation fixture only: there is no connector, no request
 * path and no account behind it, and this file must not be read as one.
 */
const remoteReference: KnowledgeReferenceView = {
  referenceId: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
  identityKey: 'demo-page-source:space-42:page-9#block-3',
  connectorName: 'Demo page source',
  sourceName: 'Team space',
  documentTitle: 'Release plan',
  documentLocation: 'Engineering / Releases',
  excerptLocation: 'page 9 · block 3',
  linkedReason: 'Holds the agreed rollout order.',
  actions: ['read'],
  scopeNote: 'Availability follows the connected source.',
}

const remoteExcerpt: KnowledgeExcerptView = {
  identityKey: remoteReference.identityKey,
  connectorName: remoteReference.connectorName,
  sourceName: remoteReference.sourceName,
  documentTitle: remoteReference.documentTitle,
  documentLocation: remoteReference.documentLocation,
  excerptLocation: remoteReference.excerptLocation,
  excerpt: 'Ship the installer after the second review.',
  excerptTruncated: false,
  freshness: 'unchanged',
  trustNote: 'External reference · read-only evidence, not Task permission',
}

function expectNoLocalOnlyVocabulary(text: string) {
  expect(text).not.toMatch(/\.md\b/)
  expect(text).not.toMatch(/\blines?\s+\d/i)
  expect(text).not.toMatch(/vault/i)
  expect(text).not.toMatch(/[A-Za-z]:\\|\/home\//)
}

test('a page/block source renders through the shared choice row with its own provenance', () => {
  render(
    <ul>
      <ReferenceChoiceRow
        disabled={false}
        limitReached={false}
        onToggle={() => {}}
        selected={false}
        view={remoteReference}
      />
    </ul>,
  )
  const row = screen.getByRole('listitem')
  expect(within(row).getByLabelText('Include Release plan from Team space')).not.toBeChecked()
  expect(within(row).getByText('Release plan')).toBeInTheDocument()
  expect(within(row).getByText('Team space · Engineering / Releases · page 9 · block 3')).toBeInTheDocument()
  expect(within(row).getByText('Linked reason')).toBeInTheDocument()
  expect(within(row).getByText('Holds the agreed rollout order.')).toBeInTheDocument()
  expectNoLocalOnlyVocabulary(row.textContent ?? '')
})

test('the shared rows offer no connection affordance for a source that is not implemented', () => {
  render(
    <ul>
      <ReferenceSummaryRow view={remoteReference} />
    </ul>,
  )
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  expect(screen.queryByText(/connect/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/sign in|account|authorize/i)).not.toBeInTheDocument()
})

test('the shared excerpt card labels remote provenance without a filesystem span', () => {
  render(<ReferenceExcerptCard view={remoteExcerpt} />)
  expect(screen.getByText('Release plan')).toBeInTheDocument()
  expect(screen.getByText('Unchanged source')).toBeInTheDocument()
  expect(screen.getByText('Team space · Engineering / Releases · page 9 · block 3')).toBeInTheDocument()
  expect(screen.getByText('Ship the installer after the second review.')).toBeInTheDocument()
  expectNoLocalOnlyVocabulary(document.body.textContent ?? '')
})

test('a selection at its limit disables the unselected rows without hiding them', () => {
  render(
    <ul>
      <ReferenceChoiceRow
        disabled={false}
        limitReached
        onToggle={() => {}}
        selected={false}
        view={remoteReference}
      />
      <ReferenceChoiceRow
        disabled={false}
        limitReached
        onToggle={() => {}}
        selected
        view={{ ...remoteReference, referenceId: 'other', documentTitle: 'Rollback plan' }}
      />
    </ul>,
  )
  expect(screen.getByLabelText('Include Release plan from Team space')).toBeDisabled()
  expect(screen.getByLabelText('Include Rollback plan from Team space')).toBeEnabled()
})
