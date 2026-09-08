import { useRef, type Dispatch, type SetStateAction } from 'react'

import { isKnowledgeCancelled } from './knowledgeErrors'
import { failState, type KnowledgePanelState, type KnowledgePending } from './knowledgeSession'

export function useKnowledgeFlight(setState: Dispatch<SetStateAction<KnowledgePanelState>>) {
  const generation = useRef(0)
  const abortRef = useRef<AbortController | null>(null)

  const still = (token: number) => token === generation.current

  const begin = (pending: KnowledgePending) => {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const token = ++generation.current
    setState((current) => ({
      ...current,
      error: null,
      errorAction: null,
      errorCode: null,
      loading: pending === 'load',
      notice: null,
      pending,
    }))
    return { signal: controller.signal, token }
  }

  const run = async <T,>(token: number, work: () => Promise<T>, apply: (value: T) => void) => {
    try {
      const value = await work()
      if (still(token)) apply(value)
    } catch (error) {
      if (still(token) && !isKnowledgeCancelled(error)) setState((current) => failState(current, error))
    }
  }

  const cancel = () => {
    abortRef.current?.abort()
    generation.current += 1
    setState((current) => ({ ...current, loading: false, pending: null }))
  }

  const forgetInFlight = () => {
    abortRef.current?.abort()
    generation.current += 1
  }

  return { abortRef, begin, cancel, forgetInFlight, run }
}
