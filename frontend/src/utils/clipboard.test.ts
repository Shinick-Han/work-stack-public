import { afterEach, expect, test, vi } from 'vitest'

import { CLIPBOARD_CANCELLED_MESSAGE, copyTextToClipboard, isClipboardCancelled } from './clipboard'

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function installExecCommand(result = true) {
  const execCommand = vi.fn(() => result)
  Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand })
  return execCommand
}

function installClipboard(writeText: unknown) {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: writeText ? { writeText } : undefined })
}

afterEach(() => {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
  Object.defineProperty(document, 'execCommand', { configurable: true, value: undefined })
})

test('a caller without a guard keeps the native write and the unchanged fallback', async () => {
  const execCommand = installExecCommand()
  const writeText = vi.fn().mockResolvedValue(undefined)
  installClipboard(writeText)
  await expect(copyTextToClipboard('native')).resolves.toBeUndefined()
  expect(writeText).toHaveBeenCalledWith('native')
  expect(execCommand).toHaveBeenCalledTimes(0)

  installClipboard(vi.fn().mockRejectedValue(new Error('permission revoked')))
  await expect(copyTextToClipboard('fallback')).resolves.toBeUndefined()
  expect(execCommand).toHaveBeenCalledTimes(1)
  expect(execCommand).toHaveBeenCalledWith('copy')
  expect(document.querySelector('textarea')).toBeNull()
})

test('an unavailable fallback still reports that the browser cannot copy', async () => {
  const execCommand = installExecCommand(false)
  installClipboard(vi.fn().mockRejectedValue(new Error('permission revoked')))
  await expect(copyTextToClipboard('value')).rejects.toThrow('Clipboard access is unavailable in this browser.')
  expect(execCommand).toHaveBeenCalledTimes(1)
  expect(document.querySelector('textarea')).toBeNull()
})

test('a guard that is already stale never starts the native write', async () => {
  const execCommand = installExecCommand()
  const writeText = vi.fn().mockResolvedValue(undefined)
  installClipboard(writeText)
  await expect(copyTextToClipboard('value', () => false)).rejects.toThrow(CLIPBOARD_CANCELLED_MESSAGE)
  expect(writeText).toHaveBeenCalledTimes(0)
  expect(execCommand).toHaveBeenCalledTimes(0)
})

test('a native rejection after the guard goes stale runs no fallback side effect', async () => {
  const execCommand = installExecCommand()
  const gate = deferred<void>()
  const writeText = vi.fn(() => gate.promise)
  installClipboard(writeText)
  let current = true
  const copying = copyTextToClipboard('value', () => current)
  await Promise.resolve()
  expect(writeText).toHaveBeenCalledTimes(1)
  current = false
  gate.reject(new Error('permission revoked'))
  await expect(copying).rejects.toThrow(CLIPBOARD_CANCELLED_MESSAGE)
  expect(execCommand).toHaveBeenCalledTimes(0)
  expect(document.querySelector('textarea')).toBeNull()
})

test('a guard that goes stale during the fallback stops before execCommand and cleans up', async () => {
  const execCommand = installExecCommand()
  installClipboard(vi.fn().mockRejectedValue(new Error('permission revoked')))
  let checks = 0
  const copying = copyTextToClipboard('value', () => {
    checks += 1
    // Current before the native write and after its rejection, stale at the
    // last check the helper makes before invoking execCommand.
    return checks < 3
  })
  await expect(copying).rejects.toSatisfy(isClipboardCancelled)
  expect(checks).toBe(3)
  expect(execCommand).toHaveBeenCalledTimes(0)
  expect(document.querySelector('textarea')).toBeNull()
})

test('a native write that already resolved is not reversed by a later stale guard', async () => {
  const execCommand = installExecCommand()
  const gate = deferred<void>()
  const writeText = vi.fn(() => gate.promise)
  installClipboard(writeText)
  let current = true
  const copying = copyTextToClipboard('value', () => current)
  await Promise.resolve()
  current = false
  gate.resolve()
  await expect(copying).resolves.toBeUndefined()
  expect(writeText).toHaveBeenCalledTimes(1)
  expect(execCommand).toHaveBeenCalledTimes(0)
})
