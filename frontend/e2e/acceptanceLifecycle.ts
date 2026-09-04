import { spawn, type ChildProcess } from 'node:child_process'
import { closeSync, mkdirSync, openSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'

/**
 * The acceptance server's ONLY parent.
 *
 * It spawns one pinned Python child with an explicit executable and argv, keeps
 * the returned ChildProcess object for the whole run, asks for an ordinary stop
 * exactly once through the run-bound request file, and proves the child left
 * through its own completion record. There is no shell, no PATH lookup, no name
 * search, no reacquisition, no kill and no restart. If start-up, readiness,
 * shutdown or cleanup cannot be proved, custody of the control root is kept and
 * the artifacts are left in place.
 *
 * Nothing in this module runs at import time.
 */

export interface AcceptanceLaunchPlan {
  readonly pythonExecutable: string
  readonly serverScript: string
  readonly projectRoot: string
  readonly controlRoot: string
  readonly runId: string
  readonly port: number
  readonly budgetSeconds: number
  readonly readyTimeoutMs: number
  readonly stopTimeoutMs: number
}

export interface ReadyRecord {
  readonly data_dir: string
  readonly dist: { readonly index_sha256: string; readonly asset_count: number }
  readonly pid: number
  readonly port: number
  readonly run_id: string
  readonly runtime_dir: string
}

export const READY_NAME = 'ready.json'
export const CHILD_LOG_NAME = 'child.log'
export const STOP_NAME = 'stop.request'
export const COMPLETION_NAME = 'completion.json'

/**
 * Explicit argv. Every value is supplied; nothing is resolved from PATH. The
 * interpreter is pinned to no bytecode writes and UTF-8 so the child never
 * litters the checkout or decodes with the host code page.
 */
export function launchArgv(plan: AcceptanceLaunchPlan): readonly string[] {
  return [
    '-B',
    '-X', 'utf8',
    plan.serverScript,
    '--port', String(plan.port),
    '--acceptance-root', plan.controlRoot,
    '--run-id', plan.runId,
    '--budget-seconds', String(plan.budgetSeconds),
  ]
}

/**
 * A ready record answers this run only, must carry a real served build and,
 * when the parent knows its child's pid, must have been written by that child.
 */
export function readyRecordAccepted(record: unknown, plan: AcceptanceLaunchPlan, expectedPid?: number): boolean {
  if (!record || typeof record !== 'object') return false
  const candidate = record as Partial<ReadyRecord>
  if (candidate.run_id !== plan.runId) return false
  if (candidate.port !== plan.port) return false
  if (typeof candidate.pid !== 'number' || candidate.pid <= 0) return false
  if (expectedPid !== undefined && candidate.pid !== expectedPid) return false
  if (typeof candidate.data_dir !== 'string' || !candidate.data_dir) return false
  if (typeof candidate.runtime_dir !== 'string' || !candidate.runtime_dir) return false
  const dist = candidate.dist
  if (!dist || typeof dist.index_sha256 !== 'string' || dist.index_sha256.length !== 64) return false
  return typeof dist.asset_count === 'number' && dist.asset_count > 0
}

/** The completion record must answer the same run and report an ordinary end. */
export function completionAccepted(record: unknown, plan: AcceptanceLaunchPlan): boolean {
  if (!record || typeof record !== 'object') return false
  const candidate = record as { run_id?: unknown; reason?: unknown }
  if (candidate.run_id !== plan.runId) return false
  return candidate.reason === 'stop-request' || candidate.reason === 'self-deadline'
}

/** An early exit is never a healthy start, whatever the code says. */
export function exitedEarly(exitCode: number | null, ready: boolean): boolean {
  return exitCode !== null && !ready
}

export function readJsonIfPresent(path: string): unknown {
  if (!existsSync(path)) return null
  try {
    return JSON.parse(readFileSync(path, 'utf-8')) as unknown
  } catch {
    return null
  }
}

export interface LifecycleLeaves {
  readonly spawnChild: (executable: string, argv: readonly string[], options: object) => ChildProcess
  readonly openLog: (path: string) => number
  readonly closeLog: (descriptor: number) => void
  readonly readJson: (path: string) => unknown
  readonly writeStop: (path: string, runId: string) => void
  readonly now: () => number
  readonly sleep: (ms: number) => Promise<void>
}

export const realLeaves: LifecycleLeaves = {
  closeLog: (descriptor) => closeSync(descriptor),
  now: () => Date.now(),
  openLog: (path) => openSync(path, 'a'),
  readJson: readJsonIfPresent,
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  spawnChild: (executable, argv, options) => spawn(executable, [...argv], options),
  writeStop: (path, runId) => writeFileSync(path, runId, { encoding: 'utf-8' }),
}

export class AcceptanceServerHandle {
  private child: ChildProcess | null = null
  private exitCode: number | null = null
  private started = false

  constructor(
    private readonly plan: AcceptanceLaunchPlan,
    private readonly leaves: LifecycleLeaves = realLeaves,
  ) {}

  get controlRoot(): string { return this.plan.controlRoot }
  get readyPath(): string { return join(this.plan.controlRoot, READY_NAME) }
  get stopPath(): string { return join(this.plan.controlRoot, STOP_NAME) }
  get completionPath(): string { return join(this.plan.controlRoot, COMPLETION_NAME) }
  /** The child's stdout and stderr are retained here, never piped and dropped. */
  get childLogPath(): string { return join(this.plan.controlRoot, CHILD_LOG_NAME) }
  /** Exposed so a failure can prove exactly one spawn happened. */
  get spawned(): boolean { return this.started }

  async start(): Promise<ReadyRecord> {
    if (this.started) throw new Error('The acceptance server was already started once.')
    this.started = true
    mkdirSync(this.plan.controlRoot, { recursive: true })
    // The child inherits a duplicate of the log descriptor; the parent's copy
    // is released right after the spawn so the file is owned by the child.
    const log = this.leaves.openLog(this.childLogPath)
    try {
      this.child = this.leaves.spawnChild(this.plan.pythonExecutable, launchArgv(this.plan), {
        cwd: this.plan.projectRoot,
        stdio: ['ignore', log, log],
        windowsHide: true,
      })
    } finally {
      this.leaves.closeLog(log)
    }
    // A signal-terminated child reports no code; that is never a healthy exit.
    this.child.on('exit', (code) => { this.exitCode = code ?? 1 })
    // A spawn failure (bad executable, EACCES) emits 'error' and may never exit.
    this.child.on('error', () => { this.exitCode = -1 })

    const deadline = this.leaves.now() + this.plan.readyTimeoutMs
    while (this.leaves.now() < deadline) {
      const record = this.leaves.readJson(this.readyPath)
      if (readyRecordAccepted(record, this.plan, this.child.pid)) return record as ReadyRecord
      if (exitedEarly(this.exitCode, false)) {
        throw new Error('The acceptance server exited before it was ready; artifacts retained.')
      }
      await this.leaves.sleep(50)
    }
    throw new Error('The acceptance server never published an acceptable ready record.')
  }

  /** One ordinary stop request, then proof of an ordinary exit. */
  async stop(): Promise<void> {
    if (!this.started) return
    this.leaves.writeStop(this.stopPath, this.plan.runId)
    const deadline = this.leaves.now() + this.plan.stopTimeoutMs
    while (this.leaves.now() < deadline) {
      if (this.exitCode !== null && completionAccepted(this.leaves.readJson(this.completionPath), this.plan)) {
        return
      }
      await this.leaves.sleep(50)
    }
    // No kill, no terminate, no name search: custody and artifacts are kept.
    throw new Error('The acceptance server did not complete its ordinary shutdown; custody retained.')
  }
}
