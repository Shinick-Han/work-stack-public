import { afterEach, describe, expect, test, vi } from 'vitest'
import { embeddedSourceHostAvailable, hideEmbeddedSource, requestEmbeddedSourceDraft, resumeEmbeddedSource, showEmbeddedSource, suspendEmbeddedSource } from './sourceHostBridge'

const originalChrome = (window as Window & { chrome?: unknown }).chrome
const originalDevicePixelRatio = window.devicePixelRatio

afterEach(() => {
  Object.defineProperty(window, 'chrome', { configurable: true, value: originalChrome })
  Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: originalDevicePixelRatio })
})

describe('desktop source host bridge', () => {
  test('is absent in the normal browser product', () => {
    Object.defineProperty(window, 'chrome', { configurable: true, value: undefined })
    expect(embeddedSourceHostAvailable()).toBe(false)
    expect(hideEmbeddedSource()).toBe(false)
    expect(suspendEmbeddedSource()).toBe(false)
    expect(resumeEmbeddedSource()).toBe(false)
  })

  test('sends only provider identity and rounded layout coordinates', () => {
    const postMessage = vi.fn()
    Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: { postMessage } } })

    expect(showEmbeddedSource('teams', { left: 22.4, top: 140.6, width: 901.7, height: 640.2 })).toBe(true)
    expect(postMessage).toHaveBeenCalledWith('workstack-source-host|show|teams|22|141|902|640')
    showEmbeddedSource('outlook', { left: -10.5, top: -20.5, width: 300, height: 200 })
    expect(postMessage).toHaveBeenLastCalledWith('workstack-source-host|show|outlook|-10|-20|300|200')
    expect(hideEmbeddedSource()).toBe(true)
    expect(postMessage).toHaveBeenLastCalledWith('workstack-source-host|hide')
    expect(suspendEmbeddedSource()).toBe(true)
    expect(postMessage).toHaveBeenLastCalledWith('workstack-source-host|suspend')
    expect(resumeEmbeddedSource()).toBe(true)
    expect(postMessage).toHaveBeenLastCalledWith('workstack-source-host|resume')
  })

  test('converts CSS pixels to native WebView coordinates at Windows display scale', () => {
    const postMessage = vi.fn()
    Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: { postMessage } } })
    Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: 1.5 })

    expect(showEmbeddedSource('outlook', { left: 400, top: 240, width: 960, height: 600 })).toBe(true)
    expect(postMessage).toHaveBeenCalledWith('workstack-source-host|show|outlook|600|360|1440|900')
  })

  test('requests the current provider URL and explicitly copied text without reading page content', async () => {
    const listeners = new Set<(event: MessageEvent) => void>()
    const postMessage = vi.fn((message: string) => {
      const requestId = message.split('|')[3]
      queueMicrotask(() => listeners.forEach((listener) => listener(new MessageEvent('message', { data: {
        type: 'workstack-source-draft',
        request_id: requestId,
        provider: 'outlook',
        url: 'https://outlook.office.com/mail/inbox/id/abc',
        title: 'Reliability review - Outlook',
        text: 'Review the reliability findings\nPrepare an owner-by-owner action plan.',
      } }))))
    })
    Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: {
      postMessage,
      addEventListener: (_type: 'message', listener: (event: MessageEvent) => void) => listeners.add(listener),
      removeEventListener: (_type: 'message', listener: (event: MessageEvent) => void) => listeners.delete(listener),
    } } })

    await expect(requestEmbeddedSourceDraft('outlook')).resolves.toEqual({
      url: 'https://outlook.office.com/mail/inbox/id/abc',
      title: 'Reliability review - Outlook',
      text: 'Review the reliability findings\nPrepare an owner-by-owner action plan.',
    })
    expect(postMessage).toHaveBeenCalledWith(expect.stringMatching(/^workstack-source-host\|capture\|outlook\|source-capture-/))
    expect(listeners.size).toBe(0)
  })
})
