import { render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { DailyReportContextPanel } from './DailyReportContextPanel'
import type { DailyReportContextCatalog } from '../../domain/reporting'

const CAPTURED_AT = '2026-09-06T01:02:03Z'

function catalog(overrides: Partial<DailyReportContextCatalog> = {}): DailyReportContextCatalog {
  return {
    captured_at: CAPTURED_AT,
    items: [{
      capture_id: 'C-0001',
      capture_revision: 1,
      title: 'Synthetic context',
      linked_task_ids: ['T-0001', 'T-0002'],
      status: 'linked',
    }],
    omitted_count: 0,
    ...overrides,
  }
}

function panel() {
  return screen.getByRole('region', { name: 'Related task context' })
}

test('shows the capture identity, title, honest status and linked tasks', () => {
  render(<DailyReportContextPanel catalog={catalog()} />)
  const section = panel()
  expect(within(section).getByText('C-0001')).toBeVisible()
  expect(within(section).getByText('Synthetic context')).toBeVisible()
  expect(within(section).getByText('linked')).toBeVisible()
  expect(within(section).getByText('Linked tasks: T-0001, T-0002')).toBeVisible()
  expect(within(section).getByText(/does not verify individual claims/)).toBeVisible()
  expect(within(section).getByText(/^Captured /)).toBeVisible()
})

test('renders a dismissed capture with its real status rather than as active', () => {
  render(<DailyReportContextPanel catalog={catalog({
    items: [{
      capture_id: 'C-0002',
      capture_revision: 4,
      title: 'Dismissed but still linked',
      linked_task_ids: ['T-0003'],
      status: 'dismissed',
    }],
  })} />)
  expect(within(panel()).getByText('dismissed')).toBeVisible()
})

test('is inert: no action control, no link, and no markup from a title', () => {
  const hostile = '<img src=x onerror="alert(1)"> [open](https://example.invalid) **bold**'
  const { container } = render(<DailyReportContextPanel catalog={catalog({
    items: [{
      capture_id: 'C-0003',
      capture_revision: 0,
      title: hostile,
      linked_task_ids: ['T-0004'],
      status: 'inbox',
    }],
  })} />)
  const section = panel()
  expect(within(section).getByText(hostile)).toBeVisible()
  expect(within(section).queryAllByRole('button')).toHaveLength(0)
  expect(within(section).queryAllByRole('link')).toHaveLength(0)
  expect(container.querySelector('img')).toBeNull()
  expect(container.querySelector('a')).toBeNull()
  expect(container.querySelector('input')).toBeNull()
  expect(container.innerHTML).not.toContain('<img')
})

test('states plainly when nothing is linked', () => {
  render(<DailyReportContextPanel catalog={catalog({ items: [] })} />)
  const section = panel()
  expect(within(section).getByText('No context is linked to these tasks.')).toBeVisible()
  expect(within(section).queryByText(/not shown here/)).toBeNull()
})

test('makes truncation visible and points at Task context for the rest', () => {
  render(<DailyReportContextPanel catalog={catalog({ omitted_count: 5 })} />)
  expect(within(panel()).getByText(
    '5 more linked captures are not shown here. Review Task context for the full list.',
  )).toBeVisible()
})

test('uses singular wording for a single omitted capture', () => {
  render(<DailyReportContextPanel catalog={catalog({ omitted_count: 1 })} />)
  expect(within(panel()).getByText(
    '1 more linked capture is not shown here. Review Task context for the full list.',
  )).toBeVisible()
})

test('gives each co-mounted panel its own heading id', () => {
  const { container } = render(
    <>
      <DailyReportContextPanel catalog={catalog()} />
      <DailyReportContextPanel catalog={catalog({
        items: [{
          capture_id: 'C-0002',
          capture_revision: 1,
          title: 'Weekly context',
          linked_task_ids: ['T-0003'],
          status: 'linked',
        }],
      })} />
    </>,
  )
  const sections = screen.getAllByRole('region', { name: 'Related task context' })
  expect(sections).toHaveLength(2)
  const headingIds = sections.map((section) => section.getAttribute('aria-labelledby'))
  expect(headingIds.every((id) => Boolean(id))).toBe(true)
  expect(new Set(headingIds).size).toBe(2)
  headingIds.forEach((id) => {
    expect(container.querySelectorAll(`#${CSS.escape(id as string)}`)).toHaveLength(1)
    expect(container.querySelector(`#${CSS.escape(id as string)}`)?.textContent)
      .toBe('Related task context')
  })
  expect(within(sections[0]).getByText('Synthetic context')).toBeVisible()
  expect(within(sections[1]).getByText('Weekly context')).toBeVisible()
})
