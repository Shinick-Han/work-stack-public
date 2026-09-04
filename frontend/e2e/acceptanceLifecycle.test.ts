import { describe, expect, it } from 'vitest'
import { EventEmitter } from 'node:events'

import {
  AcceptanceServerHandle,
  completionAccepted,
  exitedEarly,
  launchArgv,
  readyRecordAccepted,
  type AcceptanceLaunchPlan,
  type LifecycleLeaves,
} from './acceptanceLifecycle'

/**
 * Fixture contract tests. They exercise the ACTUAL lifecycle helpers with an
 * inert child and clock; no process is spawned, no browser or server is
 * started, and nothing here is browser or native evidence.
 */

const plan: AcceptanceLaunchPlan = {
  budgetSeconds: 600,
  controlRoot: 'C:/owned/control-root',
  port: 18791,
  projectRoot: 'C:/owned/project',
  pythonExecutable: 'C:/pinned/python.exe',
  readyTimeoutMs: 1000,
  runId: 'run-abc',
  serverScript: '../scripts/run_e2e_server.py',
  stopTimeoutMs: 1000,
}

const healthyReady = {
  data_dir: 'C:/owned/control-root/temp/acceptance-x',
  dist: { asset_count: 3, index_sha256: 'a'.repeat(64) },
  pid: 4242,
  port: 18791,
  run_id: 'run-abc',
  runtime_dir: 'C:/owned/control-root/runtime',
}

function inertChild(pid = 4242) {
  const child = new EventEmitter() as EventEmitter & { killed: boolean; pid: number }
  child.killed = false
  child.pid = pid
  return child
}

interface Harness {
  handle: AcceptanceServerHandle
  child: ReturnType<typeof inertChild>
  spawns: { executable: string; argv: readonly string[]; options: object }[]
  stops: { path: string; runId: string }[]
  files: Map<string, unknown>
  logs: { opened: string[]; closed: number[] }
}

function harness(overrides: Partial<LifecycleLeaves> = {}, childPid = 4242): Harness {
  const child = inertChild(childPid)
  const spawns: Harness['spawns'] = []
  const stops: Harness['stops'] = []
  const files = new Map<string, unknown>()
  let clock = 0
  const logs: { opened: string[]; closed: number[] } = { opened: [], closed: [] }
  const leaves: LifecycleLeaves = {
    closeLog: (descriptor) => { logs.closed.push(descriptor) },
    now: () => clock,
    openLog: (path) => { logs.opened.push(path); return 7 },
    readJson: (path) => files.get(path) ?? null,
    sleep: async () => { clock += 100 },
    spawnChild: (executable, argv, options) => {
      spawns.push({ argv, executable, options })
      return child as never
    },
    writeStop: (path, runId) => { stops.push({ path, runId }) },
    ...overrides,
  }
  return { child, files, handle: new AcceptanceServerHandle(plan, leaves), logs, spawns, stops }
}

describe('the launch is explicit and pinned', () => {
  it('passes the exact executable, argv, cwd and windowsHide with no shell', async () => {
    const test = harness()
    test.files.set(test.handle.readyPath, healthyReady)

    await test.handle.start()

    expect(test.spawns).toHaveLength(1)
    expect(test.spawns[0].executable).toBe('C:/pinned/python.exe')
    expect(test.spawns[0].argv).toEqual([
      '-B',
      '-X', 'utf8',
      '../scripts/run_e2e_server.py',
      '--port', '18791',
      '--acceptance-root', 'C:/owned/control-root',
      '--run-id', 'run-abc',
      '--budget-seconds', '600',
    ])
    expect(test.spawns[0].options).toMatchObject({ cwd: 'C:/owned/project', windowsHide: true })
    expect(test.spawns[0].options).not.toHaveProperty('shell')
  })

  it('retains the child output in a log under the control root and releases its own descriptor', async () => {
    const test = harness()
    test.files.set(test.handle.readyPath, healthyReady)
    await test.handle.start()
    expect(test.logs.opened).toEqual([test.handle.childLogPath])
    expect(test.spawns[0].options).toMatchObject({ stdio: ['ignore', 7, 7] })
    expect(test.logs.closed).toEqual([7])
  })

  it('builds the same argv from the pure helper', () => {
    expect(launchArgv(plan)).toContain('../scripts/run_e2e_server.py')
    expect(launchArgv(plan).slice(0, 3)).toEqual(['-B', '-X', 'utf8'])
    expect(launchArgv(plan)).toContain('--run-id')
  })

  it('refuses a second start, so exactly one child can exist', async () => {
    const test = harness()
    test.files.set(test.handle.readyPath, healthyReady)
    await test.handle.start()
    await expect(test.handle.start()).rejects.toThrow(/already started/)
    expect(test.spawns).toHaveLength(1)
  })
})

describe('the ready record is validated, never assumed', () => {
  it.each([
    ['an absent record', null],
    ['a malformed record', 'not-json'],
    ['a foreign run id', { ...healthyReady, run_id: 'other-run' }],
    ['a different port', { ...healthyReady, port: 1 }],
    ['a missing pid', { ...healthyReady, pid: 0 }],
    ['a build with no assets', { ...healthyReady, dist: { asset_count: 0, index_sha256: 'a'.repeat(64) } }],
    ['a build with no index digest', { ...healthyReady, dist: { asset_count: 2, index_sha256: 'short' } }],
  ])('rejects %s', (_name, record) => {
    expect(readyRecordAccepted(record, plan)).toBe(false)
  })

  it('accepts the healthy record', () => {
    expect(readyRecordAccepted(healthyReady, plan)).toBe(true)
  })

  it('rejects a record written by a process other than the spawned child', () => {
    expect(readyRecordAccepted(healthyReady, plan, 4243)).toBe(false)
    expect(readyRecordAccepted(healthyReady, plan, 4242)).toBe(true)
  })

  it('treats a spawn error as a failed start and never requests a stop', async () => {
    const test = harness()
    const started = test.handle.start()
    test.child.emit('error', new Error('spawn ENOENT'))
    await expect(started).rejects.toThrow(/exited before it was ready/)
    expect(test.stops).toHaveLength(0)
  })

  it('never accepts a ready record written by a process other than its own child', async () => {
    const test = harness({}, 9999)
    test.files.set(test.handle.readyPath, healthyReady)
    await expect(test.handle.start()).rejects.toThrow(/never published/)
    expect(test.stops).toHaveLength(0)
  })

  it('times out instead of proceeding when nothing acceptable ever appears', async () => {
    const test = harness()
    await expect(test.handle.start()).rejects.toThrow(/never published/)
    expect(test.stops).toHaveLength(0)
  })

  it('treats an early child exit as a failed start and never requests a stop', async () => {
    const test = harness()
    const started = test.handle.start()
    test.child.emit('exit', 1)
    await expect(started).rejects.toThrow(/exited before it was ready/)
    expect(test.stops).toHaveLength(0)
  })
})

describe('shutdown is ordinary, single and proved', () => {
  it('writes exactly one run-bound stop request and accepts the completion record', async () => {
    const test = harness()
    test.files.set(test.handle.readyPath, healthyReady)
    await test.handle.start()
    test.files.set(test.handle.completionPath, { reason: 'stop-request', run_id: 'run-abc' })
    test.child.emit('exit', 0)

    await test.handle.stop()

    expect(test.stops).toEqual([{ path: test.handle.stopPath, runId: 'run-abc' }])
  })

  it('keeps custody when the child never records an ordinary completion', async () => {
    const test = harness()
    test.files.set(test.handle.readyPath, healthyReady)
    await test.handle.start()
    test.child.emit('exit', 0)

    await expect(test.handle.stop()).rejects.toThrow(/custody retained/)
    // Still exactly one stop request, and no kill path exists at all.
    expect(test.stops).toHaveLength(1)
    expect(test.child.killed).toBe(false)
  })

  it.each([
    ['a foreign run id', { reason: 'stop-request', run_id: 'other' }],
    ['an unknown reason', { reason: 'crashed', run_id: 'run-abc' }],
    ['an absent record', null],
  ])('refuses %s as completion', (_name, record) => {
    expect(completionAccepted(record, plan)).toBe(false)
  })

  it('accepts the self-deadline as an ordinary end', () => {
    expect(completionAccepted({ reason: 'self-deadline', run_id: 'run-abc' }, plan)).toBe(true)
  })

  it('classifies an exit without readiness as early', () => {
    expect(exitedEarly(0, false)).toBe(true)
    expect(exitedEarly(null, false)).toBe(false)
    expect(exitedEarly(0, true)).toBe(false)
  })
})
