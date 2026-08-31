import { existsSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

const realViews = fileURLToPath(new URL('./src/features/workspace/views', import.meta.url))
const fallbackViews = fileURLToPath(
  new URL('./src/integration/workspaceViewsFallback.tsx', import.meta.url),
)

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // The integration branch contains the Agent C implementation. This branch keeps
      // building independently with an explicit placeholder outside Agent C ownership.
      'features/workspace/views': existsSync(realViews) ? realViews : fallbackViews,
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
    css: true,
  },
})
