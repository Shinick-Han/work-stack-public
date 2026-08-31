import type { SVGProps } from 'react'

export type IconName =
  | 'activity'
  | 'arrowUpRight'
  | 'board'
  | 'check'
  | 'chevronDown'
  | 'close'
  | 'command'
  | 'context'
  | 'graph'
  | 'inbox'
  | 'menu'
  | 'moon'
  | 'more'
  | 'plus'
  | 'refresh'
  | 'search'
  | 'spark'
  | 'sun'
  | 'target'
  | 'table'
  | 'task'
  | 'treemap'
  | 'upload'
  | 'warning'

const paths: Record<IconName, React.ReactNode> = {
  activity: <><path d="M4 12h3l2-7 4 14 2-7h5" /><path d="M4 5v14h16" opacity=".25" /></>,
  arrowUpRight: <><path d="M7 17 17 7" /><path d="M8 7h9v9" /></>,
  board: <><rect x="3" y="4" width="5" height="16" rx="1.5" /><rect x="10" y="4" width="5" height="10" rx="1.5" /><rect x="17" y="4" width="4" height="13" rx="1.5" /></>,
  check: <path d="m5 12 4 4L19 6" />,
  chevronDown: <path d="m7 10 5 5 5-5" />,
  close: <><path d="m6 6 12 12" /><path d="M18 6 6 18" /></>,
  command: <><path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3Z" /></>,
  context: <><path d="M6 3h9l3 3v15H6z" /><path d="M14 3v4h4M9 12h6M9 16h5" /></>,
  graph: <><circle cx="5" cy="12" r="2" /><circle cx="12" cy="5" r="2" /><circle cx="19" cy="9" r="2" /><circle cx="15" cy="19" r="2" /><path d="m6.5 10.5 4-4M14 6l3.5 2M18 11l-2 6M13 18l-6.5-4.5" /></>,
  inbox: <><path d="M4 4h16v15H4z" /><path d="M4 14h4l2 3h4l2-3h4" /></>,
  menu: <><path d="M4 7h16M4 12h16M4 17h16" /></>,
  moon: <path d="M20 15.4A8 8 0 0 1 8.6 4 8 8 0 1 0 20 15.4Z" />,
  more: <><circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" /></>,
  plus: <><path d="M12 5v14M5 12h14" /></>,
  refresh: <><path d="M20 7v5h-5" /><path d="M19 12a7 7 0 1 1-2-5" /></>,
  search: <><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></>,
  spark: <><path d="m12 3 1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5Z" /><path d="m18.5 15 .7 2.3 2.3.7-2.3.7-.7 2.3-.7-2.3-2.3-.7 2.3-.7Z" /></>,
  sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42" /></>,
  target: <><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="3" /><path d="M12 4V2M20 12h2" /></>,
  table: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 9h18M9 9v11M15 9v11" /></>,
  task: <><rect x="4" y="3" width="16" height="18" rx="2" /><path d="m8 9 1.5 1.5L12 8M14 9h3M8 15h9" /></>,
  treemap: <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M12 3v18M12 11h9M3 14h9" /></>,
  upload: <><path d="M12 16V4M7 9l5-5 5 5" /><path d="M5 14v6h14v-6" /></>,
  warning: <><path d="M12 3 2.8 20h18.4Z" /><path d="M12 9v5M12 17h.01" /></>,
}

interface IconProps extends SVGProps<SVGSVGElement> {
  name: IconName
  size?: number
}

export function Icon({ name, size = 18, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      viewBox="0 0 24 24"
      width={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.7"
      {...props}
    >
      {paths[name]}
    </svg>
  )
}
