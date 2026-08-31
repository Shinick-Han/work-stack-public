/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_WORKSTACK_OUTLOOK_READ_VERIFIED?: string
  readonly VITE_WORKSTACK_OUTLOOK_REPLY_VERIFIED?: string
  readonly VITE_WORKSTACK_TEAMS_READ_VERIFIED?: string
  readonly VITE_WORKSTACK_TEAMS_REPLY_VERIFIED?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
