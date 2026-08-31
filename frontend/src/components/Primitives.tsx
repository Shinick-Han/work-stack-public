import type { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from 'react'
import { Icon, type IconName } from './Icon'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon?: IconName
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
}

export function Button({
  children,
  className = '',
  icon,
  variant = 'secondary',
  type = 'button',
  ...props
}: ButtonProps) {
  return (
    <button className={`button button--${variant} ${className}`} type={type} {...props}>
      {icon ? <Icon name={icon} size={16} /> : null}
      {children}
    </button>
  )
}

export function IconButton({
  label,
  icon,
  ...props
}: Omit<ButtonProps, 'children'> & { label: string; icon: IconName }) {
  return (
    <Button aria-label={label} className="icon-button" icon={icon} title={label} {...props} />
  )
}

export function Pill({ children, tone = 'neutral' }: PropsWithChildren<{ tone?: string }>) {
  return <span className={`pill pill--${tone}`}>{children}</span>
}

export function EmptyState({
  icon = 'spark',
  title,
  children,
  action,
}: PropsWithChildren<{ icon?: IconName; title: string; action?: ReactNode }>) {
  return (
    <div className="empty-state">
      <span className="empty-state__icon"><Icon name={icon} size={22} /></span>
      <h2>{title}</h2>
      <div className="empty-state__copy">{children}</div>
      {action ? <div className="empty-state__action">{action}</div> : null}
    </div>
  )
}

export function LoadingBlock({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="loading-block" role="status">
      <span className="spinner" />
      <span>{label}</span>
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-state" role="alert">
      <span className="error-state__icon"><Icon name="warning" size={20} /></span>
      <div>
        <strong>Couldn’t load this view</strong>
        <p>{message}</p>
      </div>
      {onRetry ? <Button onClick={onRetry} icon="refresh">Try again</Button> : null}
    </div>
  )
}
