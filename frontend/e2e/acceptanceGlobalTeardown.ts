import type { AcceptanceServerHandle } from './acceptanceLifecycle'

/** One run-bound stop request, proved by the child's completion record. */
export default async function globalTeardown(): Promise<void> {
  const scope = globalThis as { __workstackAcceptanceHandle?: AcceptanceServerHandle }
  const handle = scope.__workstackAcceptanceHandle
  scope.__workstackAcceptanceHandle = undefined
  if (handle) await handle.stop()
}
