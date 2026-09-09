import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { KnowledgeEvidencePanel } from './KnowledgeEvidencePanel'
import {
  crowdedEvidence,
  emptyEvidence,
  evidenceRow,
  evidenceRows,
  unversionedEvidence,
  verifiedEvidence,
} from './knowledgeEvidenceFixture'

const RTL_OVERRIDE = String.fromCharCode(0x202e)
const ZERO_WIDTH = String.fromCharCode(0x200b)
const BELL = String.fromCharCode(0x0007)

function panelText() {
  return screen.getByRole('region', { name: 'Imported evidence' }).textContent ?? ''
}

test('withholds a current source state until every item says which version it was read at', () => {
  const { unmount } = render(<KnowledgeEvidencePanel evidence={verifiedEvidence} />)
  expect(screen.getByText('Source checked · current')).toBeInTheDocument()
  unmount()

  render(<KnowledgeEvidencePanel evidence={unversionedEvidence} />)
  expect(screen.queryByText('Source checked · current')).not.toBeInTheDocument()
  expect(screen.getByText('Not verified')).toBeInTheDocument()
  expect(panelText()).toContain('do not all say which version')
  expect(screen.getAllByText('Version not supplied')).toHaveLength(3)
})

test('withholds current when a supplied row past the display cap reports no version', () => {
  const rows = [...evidenceRows(10), evidenceRow(11, false)]
  render(
    <KnowledgeEvidencePanel evidence={{ ...verifiedEvidence, evidenceCount: 11, rows }} />,
  )

  // The unversioned row never reaches the screen, which is exactly why it cannot be
  // assumed good: the reader has no way to notice the gap for themselves.
  expect(screen.getAllByRole('listitem').filter((item) => item.className.includes('evidence-item'))).toHaveLength(10)
  expect(panelText()).not.toContain('Rollback step 11')
  expect(screen.queryByText('Source checked · current')).not.toBeInTheDocument()
  expect(screen.getByText('Not verified')).toBeInTheDocument()
  expect(panelText()).toContain('do not all say which version')
})

test('reports a supplied unhealthy state as given and never upgrades it', () => {
  render(
    <KnowledgeEvidencePanel evidence={{ ...verifiedEvidence, verification: 'deleted' }} />,
  )
  expect(screen.getByText('Source no longer exists')).toBeInTheDocument()
  expect(screen.queryByText('Source checked · current')).not.toBeInTheDocument()
})

test('an unreadable state and an unknown state both fall back to unverified copy', () => {
  const { unmount } = render(<KnowledgeEvidencePanel evidence={{ ...verifiedEvidence, verification: undefined }} />)
  expect(screen.getByText('Not verified')).toBeInTheDocument()
  unmount()

  // A state this build does not know about must not be shown as if it were good news.
  render(
    <KnowledgeEvidencePanel
      evidence={{ ...verifiedEvidence, verification: 'totally-fine' as never }}
    />,
  )
  expect(screen.getByText('Not verified')).toBeInTheDocument()
})

test('strips display-spoofing characters and never promotes an opaque id to a title', () => {
  const opaqueId = `urn:evidence:${'9f3a'.repeat(60)}`
  render(
    <KnowledgeEvidencePanel
      evidence={{
        ...verifiedEvidence,
        sourceLabel: `Team${RTL_OVERRIDE} knowledge${ZERO_WIDTH} base`,
        rows: [
          { id: opaqueId, title: `  Rollback${BELL} plan${RTL_OVERRIDE}  `, typeLabel: 'Page', versionLabel: 'v4' },
          { id: opaqueId, title: '   ', typeLabel: null, versionLabel: 'v5' },
          { id: 'evidence-long', title: 'x'.repeat(400), typeLabel: 'Answer', versionLabel: 'v6' },
        ],
      }}
    />,
  )

  const text = panelText()
  expect(text).not.toContain(RTL_OVERRIDE)
  expect(text).not.toContain(ZERO_WIDTH)
  expect(text).not.toContain(BELL)
  expect(text).not.toContain(opaqueId)
  expect(screen.getByText('Team knowledge base')).toBeInTheDocument()
  expect(screen.getByText('Rollback plan')).toBeInTheDocument()
  expect(screen.getByText('Untitled item')).toBeInTheDocument()
  // A long title is clamped for the drawer rather than rendered whole.
  expect(screen.getByText(/^x+…$/).textContent).toHaveLength(161)
})

test('escapes supplied text and builds nothing executable from it when a presenter breaks contract', () => {
  // A raw locator and a markup fragment are contract violations, not acceptable labels:
  // the view model requires presenter-authored display copy. They appear here to pin down
  // what the panel does when one arrives anyway — the text stays inert and no anchor,
  // image or request is constructed from it — not to bless them as legitimate input.
  const markup = '<img src=x onerror="alert(1)">'
  const rawLocator = 'file:///C:/nas/private/plan.docx'
  const { container } = render(
    <KnowledgeEvidencePanel
      evidence={{
        ...verifiedEvidence,
        sourceLabel: markup,
        rows: [{ id: 'a', title: rawLocator, typeLabel: 'Document', versionLabel: 'v1' }],
      }}
    />,
  )
  expect(container.querySelectorAll('a')).toHaveLength(0)
  expect(container.querySelectorAll('img, iframe, script')).toHaveLength(0)
  // Both render as the literal characters they are, escaped by React rather than parsed.
  expect(screen.getByText(markup)).toBeInTheDocument()
  expect(screen.getByText(rawLocator)).toBeInTheDocument()
})

test('offers no way to open the source unless a callback and a permission both arrive', () => {
  const onOpenSource = vi.fn()
  const { unmount } = render(<KnowledgeEvidencePanel evidence={verifiedEvidence} />)
  expect(screen.queryByRole('button', { name: 'Open the original source' })).not.toBeInTheDocument()
  unmount()

  const withoutPermission = render(
    <KnowledgeEvidencePanel evidence={verifiedEvidence} onOpenSource={onOpenSource} />,
  )
  const blocked = screen.getByRole('button', { name: 'Open the original source' })
  expect(blocked).toBeDisabled()
  fireEvent.click(blocked)
  expect(onOpenSource).not.toHaveBeenCalled()
  expect(panelText()).toContain('not available for this evidence yet')
  withoutPermission.unmount()

  render(
    <KnowledgeEvidencePanel
      evidence={verifiedEvidence}
      onOpenSource={onOpenSource}
      openSourcePermitted
    />,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Open the original source' }))
  expect(onOpenSource).toHaveBeenCalledTimes(1)
})

test('bounds the listed items and says what was left out', () => {
  render(<KnowledgeEvidencePanel evidence={crowdedEvidence} />)
  expect(screen.getAllByRole('listitem').filter((item) => item.className.includes('evidence-item'))).toHaveLength(10)
  expect(panelText()).toContain('Showing 10 of 24 items')
})

test('warns about truncated and conflicting results only when the retrieval flagged them', () => {
  const { unmount } = render(<KnowledgeEvidencePanel evidence={verifiedEvidence} />)
  expect(panelText()).not.toContain('Some matches were left out')
  expect(panelText()).not.toContain('disagree with each other')
  unmount()

  render(<KnowledgeEvidencePanel evidence={crowdedEvidence} />)
  expect(panelText()).toContain('Some matches were left out')
  expect(panelText()).toContain('disagree with each other')
})

test('shows retrieval confidence as a match score, and shows nothing when none was supplied', () => {
  const { unmount } = render(<KnowledgeEvidencePanel evidence={verifiedEvidence} />)
  expect(screen.getByText('Retrieval confidence')).toBeInTheDocument()
  expect(screen.getByText('Match score 0.82')).toBeInTheDocument()
  expect(panelText()).toContain('not a judgement that the content is correct')
  unmount()

  render(<KnowledgeEvidencePanel evidence={unversionedEvidence} />)
  expect(screen.queryByText('Retrieval confidence')).not.toBeInTheDocument()
  expect(panelText()).not.toContain('not a judgement that the content is correct')
})

test('a nonsense confidence score is dropped rather than rendered as a number', () => {
  render(
    <KnowledgeEvidencePanel
      evidence={{ ...verifiedEvidence, confidenceLabel: 'Medium', confidenceScore: Number.NaN }}
    />,
  )
  expect(screen.getByText('Medium')).toBeInTheDocument()
  expect(panelText()).not.toContain('Match score')
})

test('states plainly that nothing came back instead of implying an empty source is fine', () => {
  render(<KnowledgeEvidencePanel evidence={emptyEvidence} />)
  expect(panelText()).toContain('No evidence items were supplied')
  expect(screen.getByText('No permission to open the source')).toBeInTheDocument()
  expect(panelText()).not.toContain('Showing')
})

test('trusts the supplied count over the rows it was handed', () => {
  render(
    <KnowledgeEvidencePanel evidence={{ ...verifiedEvidence, evidenceCount: 9, rows: evidenceRows(2) }} />,
  )
  expect(screen.getByText('9')).toBeInTheDocument()
  expect(panelText()).toContain('Showing 2 of 9 items')
})
