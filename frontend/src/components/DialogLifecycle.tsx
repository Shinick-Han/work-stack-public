import { createContext, useContext, type PropsWithChildren } from 'react'

export interface DialogLifecycle {
  resume: () => void
  suspend: () => boolean
}

const defaultLifecycle: DialogLifecycle = {
  resume: () => undefined,
  suspend: () => false,
}

const DialogLifecycleContext = createContext<DialogLifecycle>(defaultLifecycle)

export function DialogLifecycleProvider({ children, lifecycle }: PropsWithChildren<{ lifecycle: DialogLifecycle }>) {
  return (
    <DialogLifecycleContext.Provider value={lifecycle}>
      {children}
    </DialogLifecycleContext.Provider>
  )
}

export function useDialogLifecycle() {
  return useContext(DialogLifecycleContext)
}
