import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { capture, workspace } from '../test/fixtures'
import { ReviewSurface } from './AppSurfaces'

vi.mock('../features/review/DailyReviewPage', () => ({
  DailyReviewPage: ({ captures }: { captures: unknown[] }) => (
    <div data-testid="review-surface">review captures: {captures.length}</div>
  ),
}))

test('composes the daily review surface with the authoritative capture collection', () => {
  render(
    <ReviewSurface
      captures={[capture]}
      onNotice={vi.fn()}
      onOpenCapture={vi.fn()}
      onOpenTask={vi.fn()}
      workspace={workspace}
    />,
  )

  expect(screen.getByTestId('review-surface')).toHaveTextContent('review captures: 1')
})
