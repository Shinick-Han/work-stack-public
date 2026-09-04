import markUrl from '../assets/WorkStack-Mark-Lime-v2.svg'

interface BrandMarkProps {
  /**
   * Accessible name. Omit it for the common decorative case, where the mark
   * sits beside the visible product name and would otherwise be read twice.
   */
  label?: string
  size?: number
}

/**
 * The one Work Stack mark. Its colors are fixed in the generated asset rather
 * than themed, so light and dark surfaces keep the same brand identity while
 * everything around the mark still follows the theme.
 */
export function BrandMark({ label, size = 29 }: BrandMarkProps) {
  const decorative = label === undefined
  return <img
    alt={decorative ? '' : label}
    aria-hidden={decorative ? true : undefined}
    className="brand-mark"
    draggable={false}
    height={size}
    role={decorative ? 'presentation' : 'img'}
    src={markUrl}
    width={size}
  />
}
