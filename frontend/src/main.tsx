import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from './app/App'
import { DialogLifecycleProvider } from './components/DialogLifecycle'
import { resumeEmbeddedSource, suspendEmbeddedSource } from './features/inbox/sourceHostBridge'
import './generated/theme-tokens.css'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
    mutations: {
      retry: false,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <DialogLifecycleProvider lifecycle={{ resume: resumeEmbeddedSource, suspend: suspendEmbeddedSource }}>
        <App />
      </DialogLifecycleProvider>
    </QueryClientProvider>
  </StrictMode>,
)
