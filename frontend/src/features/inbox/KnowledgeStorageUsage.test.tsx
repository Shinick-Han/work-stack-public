import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { KnowledgeStorageUsage } from './KnowledgeStorageUsage'

/**
 * The region on its own. It is handed numbers and renders them; there is nothing else to
 * test, which is the point of keeping it out of the launcher.
 */

const usage = {
  byte_bound: 262144,
  encoded_bytes: 18432,
  request_bound: 200,
  request_count: 7,
} as const

function bars() {
  return screen.getAllByRole('progressbar')
}

test('shows both bars with their exact counts and bytes, as of the last read or save', () => {
  render(<KnowledgeStorageUsage outdated={false} usage={usage} />)

  const region = screen.getByRole('region', { name: 'Search request storage' })
  expect(region).toBeInTheDocument()

  const [requests, size] = bars()
  expect(requests).toHaveAttribute('aria-label', 'Stored requests')
  expect(requests).toHaveAttribute('aria-valuenow', '7')
  expect(requests).toHaveAttribute('aria-valuemin', '0')
  expect(requests).toHaveAttribute('aria-valuemax', '200')
  expect(requests).toHaveAttribute('aria-valuetext', '7 of 200 requests')
  expect(size).toHaveAttribute('aria-label', 'Stored size')
  expect(size).toHaveAttribute('aria-valuenow', '18432')
  expect(size).toHaveAttribute('aria-valuemax', '262144')
  expect(size).toHaveAttribute('aria-valuetext', '18,432 of 262,144 bytes')

  // The same exact figures are readable without a screen reader.
  expect(screen.getByText('7 of 200 requests')).toBeInTheDocument()
  expect(screen.getByText('18,432 of 262,144 bytes')).toBeInTheDocument()

  expect(screen.getByText('At last policy read/save')).toBeInTheDocument()
  expect(screen.getByText(/completed and expired requests/)).toBeInTheDocument()
  expect(screen.getByText(/before the request count does/)).toBeInTheDocument()

  // No promise about future searches, and nothing to press.
  expect(screen.queryByText(/searches left/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/remaining/i)).not.toBeInTheDocument()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  expect(screen.queryByText('Storage limit reached at last read/save')).not.toBeInTheDocument()
})

test('says usage is unavailable rather than showing an empty store', () => {
  render(<KnowledgeStorageUsage outdated={false} usage={null} />)

  expect(screen.getByText('Storage usage unavailable')).toBeInTheDocument()
  expect(screen.queryAllByRole('progressbar')).toHaveLength(0)
  // Absent is not zero, and it is certainly not "0 of 200".
  expect(screen.queryByText(/of 200 requests/)).not.toBeInTheDocument()
  expect(screen.queryByText('At last policy read/save')).not.toBeInTheDocument()
})

test('marks retained numbers out of date instead of passing them off as current', () => {
  render(<KnowledgeStorageUsage outdated usage={usage} />)

  expect(screen.getByText(/Out of date/)).toBeInTheDocument()
  expect(screen.queryByText('At last policy read/save')).not.toBeInTheDocument()
  // The numbers are still shown — they are what the last successful read saw.
  expect(screen.getByText('7 of 200 requests')).toBeInTheDocument()
})

test('reports an observed bound without proposing a deletion', () => {
  render(
    <KnowledgeStorageUsage outdated={false} usage={{ ...usage, request_count: 200 }} />,
  )

  expect(screen.getByText('Storage limit reached at last read/save')).toBeInTheDocument()
  expect(bars()[0]).toHaveAttribute('aria-valuetext', '200 of 200 requests')
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  expect(screen.queryByText(/delete/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/clean up/i)).not.toBeInTheDocument()
})

test('the byte bound alone is enough to report the limit', () => {
  render(
    <KnowledgeStorageUsage outdated={false} usage={{ ...usage, encoded_bytes: 262144 }} />,
  )

  expect(screen.getByText('Storage limit reached at last read/save')).toBeInTheDocument()
  expect(screen.getByText('7 of 200 requests')).toBeInTheDocument()
})
