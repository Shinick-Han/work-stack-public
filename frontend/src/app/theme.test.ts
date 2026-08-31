import { afterEach, describe, expect, test, vi } from 'vitest'
import { applyTheme, readTheme, THEME_STORAGE_KEY } from './theme'

const originalChrome = (window as Window & { chrome?: unknown }).chrome

afterEach(() => {
  window.localStorage.clear()
  document.documentElement.dataset.theme = 'dark'
  document.documentElement.style.colorScheme = ''
  Object.defineProperty(window, 'chrome', { configurable: true, value: originalChrome })
})

describe('Work Stack theme', () => {
  test('defaults to dark and restores an explicit light preference', () => {
    expect(readTheme()).toBe('dark')
    window.localStorage.setItem(THEME_STORAGE_KEY, 'light')
    expect(readTheme()).toBe('light')
  })

  test('applies the theme and keeps the native title bar in sync', () => {
    const postMessage = vi.fn()
    Object.defineProperty(window, 'chrome', { configurable: true, value: { webview: { postMessage } } })

    applyTheme('light')

    expect(document.documentElement.dataset.theme).toBe('light')
    expect(document.documentElement.style.colorScheme).toBe('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    expect(postMessage).toHaveBeenCalledWith('workstack-window-theme|light')
  })
})
