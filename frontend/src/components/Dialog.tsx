import { useEffect, useId, useRef, type PropsWithChildren, type ReactNode } from 'react'
import { resumeEmbeddedSource, suspendEmbeddedSource } from '../features/inbox/sourceHostBridge'
import { IconButton } from './Primitives'

interface DialogProps {
  open: boolean
  title: string
  description?: string
  onClose: () => void
  footer?: ReactNode
  size?: 'small' | 'medium' | 'large'
}

export function Dialog({
  children,
  description,
  footer,
  onClose,
  open,
  size = 'medium',
  title,
}: PropsWithChildren<DialogProps>) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()

  useEffect(() => {
    if (!open || !suspendEmbeddedSource()) return
    return () => { resumeEmbeddedSource() }
  }, [open])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const handleCancel = (event: Event) => {
      event.preventDefault()
      onClose()
    }
    dialog.addEventListener('cancel', handleCancel)
    return () => dialog.removeEventListener('cancel', handleCancel)
  }, [onClose])

  return (
    <dialog aria-labelledby={titleId} className={`dialog dialog--${size}`} ref={dialogRef} onClick={(event) => {
      if (event.target === dialogRef.current) onClose()
    }}>
      <div className="dialog__surface">
        <header className="dialog__header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          <IconButton icon="close" label="Close dialog" onClick={onClose} variant="ghost" />
        </header>
        <div className="dialog__body">{children}</div>
        {footer ? <footer className="dialog__footer">{footer}</footer> : null}
      </div>
    </dialog>
  )
}
