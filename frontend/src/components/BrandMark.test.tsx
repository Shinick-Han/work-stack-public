import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'

import { BrandMark } from './BrandMark'

test('is decorative by default so the visible product name is not read twice', () => {
  const { container } = render(<><BrandMark /><span>Work Stack</span></>)

  const mark = container.querySelector('img')
  expect(mark).not.toBeNull()
  expect(mark).toHaveAttribute('alt', '')
  expect(mark).toHaveAttribute('aria-hidden', 'true')
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
})

test('exposes an accessible name only when one is supplied', () => {
  render(<BrandMark label="Work Stack" />)

  const mark = screen.getByRole('img', { name: 'Work Stack' })
  expect(mark).not.toHaveAttribute('aria-hidden')
})

test('renders the generated asset at the requested size with the frozen geometry', () => {
  const { container } = render(<BrandMark size={44} />)

  const mark = container.querySelector('img')
  expect(mark).toHaveAttribute('width', '44')
  expect(mark).toHaveAttribute('height', '44')
  // The build inlines this small asset, so assert the geometry and the fixed
  // brand colors it actually carries rather than a bundled file name.
  const source = decodeURIComponent(mark?.getAttribute('src') ?? '')
  expect(source).toContain("viewBox='0 0 256 256'")
  expect(source).toContain("x='116' y='60' width='28' height='136' rx='14'")
  expect(source).toContain('#B8F24B')
  expect(source).toContain('#12150D')
  expect(source.toUpperCase()).not.toContain('#FFFF00')
})

test('keeps the shared class so the surrounding layout is unchanged', () => {
  const { container } = render(<BrandMark />)

  expect(container.querySelector('img')).toHaveClass('brand-mark')
})
