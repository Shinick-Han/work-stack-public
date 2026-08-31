export type WorkStackTheme = 'dark' | 'light'

export const THEME_STORAGE_KEY = 'workstack.theme'

interface ThemeHostWindow extends Window {
  chrome?: {
    webview?: {
      postMessage: (message: string) => void
    }
  }
}

export function readTheme(): WorkStackTheme {
  return window.localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark'
}

export function applyTheme(theme: WorkStackTheme) {
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
  window.localStorage.setItem(THEME_STORAGE_KEY, theme)
  ;(window as ThemeHostWindow).chrome?.webview?.postMessage(`workstack-window-theme|${theme}`)
}
