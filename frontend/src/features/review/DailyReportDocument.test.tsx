import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { DailyReportDocument, decodeInertText } from './DailyReportDocument'

test('decodes escaped punctuation and HTML entities into visible text', () => {
  expect(decodeInertText('\\*em\\* &lt;script&gt; person\\@example\\.com')).toBe(
    '*em* <script> person@example.com',
  )
  expect(decodeInertText('T\\-0001 leftover\\\\')).toBe('T-0001 leftover\\')
})

test('renders daily-v1 headings and lists as React text without links or HTML', () => {
  const markdown = [
    '# Daily review 2026-08-30',
    '',
    '## Day record',
    '',
    '### T\\-0001 — title',
    '',
    'Done',
    '- \\[pwn\\]\\(javascript\\:alert\\(1\\)\\) &lt;script&gt; ![img](http://evil.example/x)',
    '- person\\@example\\.com',
    '',
  ].join('\n')
  const { container } = render(<DailyReportDocument markdown={markdown} />)
  expect(screen.getByRole('heading', { level: 1, name: 'Daily review 2026-08-30' })).toBeVisible()
  expect(screen.getByRole('heading', { level: 3, name: 'T-0001 — title' })).toBeVisible()
  expect(screen.getByText('[pwn](javascript:alert(1)) <script> ![img](http://evil.example/x)')).toBeVisible()
  expect(screen.getByText('person@example.com')).toBeVisible()
  expect(container.querySelectorAll('a, img, script')).toHaveLength(0)
  expect(container.innerHTML).not.toContain('<script>')
})
