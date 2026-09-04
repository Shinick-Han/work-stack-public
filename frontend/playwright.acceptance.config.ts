import { defineConfig, devices } from '@playwright/test'
import { fileURLToPath, URL } from 'node:url'

import type { AcceptanceLaunchPlan } from './e2e/acceptanceLifecycle'

/**
 * The acceptance lane's own configuration.
 *
 * It selects ONLY e2e/acceptance.spec.ts, one Chromium project, one worker, no
 * file parallelism and unconditionally zero retries - the historical suite and
 * the CI retry are deliberately not inherited. It does NOT declare a webServer:
 * the reviewed lifecycle module, driven from e2e/acceptanceGlobalSetup.ts and
 * e2e/acceptanceGlobalTeardown.ts, is the single owner of the child, so
 * start-up, readiness, ordinary shutdown and cleanup are proved, not assumed.
 *
 * Everything here is inert at import time. The result and cache roots, the
 * pinned interpreter and the control root arrive through environment values the
 * execution packet supplies; nothing is spawned, resolved from PATH or created
 * while this module is merely loaded.
 */

const projectRoot = fileURLToPath(new URL('..', import.meta.url))
const port = Number(process.env.WORKSTACK_ACCEPTANCE_PORT ?? 18791)
const outputRoot = process.env.WORKSTACK_ACCEPTANCE_RESULTS ?? ''
const controlRoot = process.env.WORKSTACK_ACCEPTANCE_CONTROL_ROOT ?? ''
const pythonExecutable = process.env.WORKSTACK_ACCEPTANCE_PYTHON ?? ''
const runId = process.env.WORKSTACK_ACCEPTANCE_RUN_ID ?? ''

export function acceptancePlan(): AcceptanceLaunchPlan {
  // Called by setup, never at import: an execution packet must supply all four.
  for (const [name, value] of Object.entries({ controlRoot, outputRoot, pythonExecutable, runId })) {
    if (!value) throw new Error(`The acceptance lane requires ${name}; refusing to guess it.`)
  }
  return {
    // The child self-deadline must outlive the whole suite (globalTimeout below)
    // so a healthy run never loses its server mid-way; the deadline is the
    // backstop for an abandoned run, not the suite clock.
    budgetSeconds: 960,
    controlRoot,
    port,
    projectRoot,
    pythonExecutable,
    readyTimeoutMs: 30_000,
    runId,
    serverScript: 'scripts/run_e2e_server.py',
    stopTimeoutMs: 20_000,
  }
}

export default defineConfig({
  testDir: './e2e',
  testMatch: 'acceptance.spec.ts',
  globalSetup: './e2e/acceptanceGlobalSetup.ts',
  globalTeardown: './e2e/acceptanceGlobalTeardown.ts',
  fullyParallel: false,
  forbidOnly: true,
  // Unconditional: a retry would hide a real first-run failure.
  retries: 0,
  workers: 1,
  timeout: 60_000,
  globalTimeout: 900_000,
  reporter: [['list']],
  outputDir: outputRoot ? `${outputRoot}/playwright-output` : undefined,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
