import { acceptancePlan } from '../playwright.acceptance.config'
import { AcceptanceServerHandle } from './acceptanceLifecycle'

/**
 * Playwright global setup for the acceptance lane: the single owner of the
 * Python child. The handle is parked on globalThis so the separate teardown
 * module (same worker process) can request the one ordinary stop.
 */
export default async function globalSetup(): Promise<void> {
  const handle = new AcceptanceServerHandle(acceptancePlan())
  ;(globalThis as { __workstackAcceptanceHandle?: AcceptanceServerHandle }).__workstackAcceptanceHandle = handle
  await handle.start()
}
