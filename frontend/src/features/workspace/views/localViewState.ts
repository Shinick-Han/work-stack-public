/**
 * Non-React local-view persistence adapter (T-0048 shared spine).
 * View modules own payload codecs; this file owns the public store contract,
 * bounds, revision stamps, and per-view reset. Envelope codec, storage access,
 * and `storage` event subscription live in `localViewStoreSupport`.
 */

import {
  commitLocalViewStoreUpdate,
  createLocalViewStorageAdapter,
  createLocalViewStorageSubscription,
  parseLocalViewEnvelope,
  readLocalViewStoreLatest,
  type LocalViewStoreState,
} from "./localViewStoreSupport"

export const LOCAL_VIEW_SCHEMA_VERSION = 1
export const LOCAL_VIEW_RECORD_LIMIT = 262_144
export const LOCAL_VIEW_WORKSPACE_ID_LIMIT = 128
export const LOCAL_VIEW_WRITER_ID_LIMIT = 64
export const LOCAL_VIEW_PAYLOAD_ID_LIMIT = 256
export const LOCAL_VIEW_KEY_PREFIX = "workstack:local-view:v1"

export const LAYOUT_SAVE_FAILURE_MESSAGE =
  "The layout could not be saved on this device."

export const LOCAL_VIEW_NAMES = ["graph", "treemap", "table"] as const
export type LocalViewName = (typeof LOCAL_VIEW_NAMES)[number]

export type LocalViewEnvelope<V extends LocalViewName, D> = {
  schemaVersion: 1
  workspaceId: string
  view: V
  revision: number
  writtenAt: number
  writerId: string
  data: D
}

export type LocalViewStore<D> = {
  read(): D
  update(mutator: (current: D) => D): D
  reset(): D
  subscribe(listener: (data: D) => void): () => void
}

export function localViewStorageKey(workspaceId: string, view: LocalViewName): string {
  return `${LOCAL_VIEW_KEY_PREFIX}:${encodeURIComponent(workspaceId)}:${view}`
}

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

export function exactPlainObject(
  value: unknown,
  keys: readonly string[],
): Record<string, unknown> | null {
  if (!isPlainObject(value)) return null
  const actual = Object.keys(value)
  if (actual.length !== keys.length) return null
  const expected = new Set(keys)
  for (const key of actual) {
    if (!expected.has(key)) return null
  }
  return value
}

export function isPayloadId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= LOCAL_VIEW_PAYLOAD_ID_LIMIT
}

export function uniqueStringArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null
  const ids: string[] = []
  const seen = new Set<string>()
  for (const item of value) {
    if (!isPayloadId(item) || seen.has(item)) return null
    seen.add(item)
    ids.push(item)
  }
  return ids
}

export function isValidWorkspaceId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= LOCAL_VIEW_WORKSPACE_ID_LIMIT
}

export function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

/**
 * Deduplicate stored IDs, drop only proven-deleted identities, then append
 * canonical IDs the store has not seen. Shared by Treemap scopes and Table.
 */
export function normalizeStoredOrder(
  stored: readonly string[],
  canonicalIds: readonly string[],
): string[] {
  const canonical = new Set(canonicalIds)
  const seen = new Set<string>()
  const kept: string[] = []
  for (const id of stored) {
    if (!canonical.has(id) || seen.has(id)) continue
    seen.add(id)
    kept.push(id)
  }
  for (const id of canonicalIds) {
    if (seen.has(id)) continue
    seen.add(id)
    kept.push(id)
  }
  return kept
}

/**
 * Replace only currently visible slots in a normalized total order. Hidden
 * identities keep their relative positions; filter changes never need a write.
 */
export function applyVisibleReorder(
  stored: readonly string[],
  canonicalIds: readonly string[],
  visibleIds: readonly string[],
  nextVisibleIds: readonly string[],
): string[] {
  const normalized = normalizeStoredOrder(stored, canonicalIds)
  const visible = new Set(visibleIds)
  let cursor = 0
  return normalized.map((id) => (visible.has(id) ? nextVisibleIds[cursor++] ?? id : id))
}

export function createWriterId(): string {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID().slice(0, LOCAL_VIEW_WRITER_ID_LIMIT)
    }
  } catch {
    // Fall through to a non-crypto tab stamp.
  }
  return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`.slice(
    0,
    LOCAL_VIEW_WRITER_ID_LIMIT,
  )
}

function isLocalViewName(value: unknown): value is LocalViewName {
  return value === "graph" || value === "treemap" || value === "table"
}

export function createExternalUpdateGate<D>(apply: (data: D) => void) {
  let gesture = false
  let buffered: D | null = null
  return {
    beginGesture() {
      gesture = true
    },
    onExternal(data: D) {
      if (gesture) {
        buffered = data
        return "buffered" as const
      }
      apply(data)
      return "applied" as const
    },
    cancelGesture() {
      gesture = false
      const value = buffered
      buffered = null
      if (value !== null) apply(value)
      return value
    },
    endGesture() {
      gesture = false
      buffered = null
    },
    get buffered() {
      return buffered
    },
    get active() {
      return gesture
    },
  }
}

export function createLocalViewStore<V extends LocalViewName, D>(options: {
  workspaceId: string
  view: V
  defaultData: D
  parseData: (value: unknown) => D | null
  onPersistFailure?: () => void
}): LocalViewStore<D> {
  const persistable = isValidWorkspaceId(options.workspaceId) && isLocalViewName(options.view)
  const key = persistable ? localViewStorageKey(options.workspaceId, options.view) : null
  const writerId = createWriterId()
  const fallback = () => cloneJson(options.defaultData)
  const listeners = new Set<(data: D) => void>()
  const state: LocalViewStoreState<D> = { memory: fallback(), revision: 0, unsaved: false }
  let announcedFailure = false
  const emit = (data: D) => {
    for (const listener of [...listeners]) listener(cloneJson(data))
  }
  const announceFailure = () => {
    if (announcedFailure) return
    announcedFailure = true
    options.onPersistFailure?.()
  }
  const parseEnvelope = (raw: string) => parseLocalViewEnvelope(raw, {
    workspaceId: options.workspaceId,
    view: options.view,
    schemaVersion: LOCAL_VIEW_SCHEMA_VERSION,
    recordLimit: LOCAL_VIEW_RECORD_LIMIT,
    writerIdLimit: LOCAL_VIEW_WRITER_ID_LIMIT,
    exactPlainObject,
    isValidWorkspaceId,
    parseData: options.parseData,
  })
  const adapter = createLocalViewStorageAdapter({
    key,
    recordLimit: LOCAL_VIEW_RECORD_LIMIT,
    parseEnvelope,
    announceFailure,
    schemaVersion: LOCAL_VIEW_SCHEMA_VERSION,
    workspaceId: options.workspaceId,
    view: options.view,
    writerId,
  })
  const latest = () => readLocalViewStoreLatest({
    state, readRecord: adapter.readRecord, fallback, cloneJson,
  })
  const subscription = createLocalViewStorageSubscription({
    key, state, parseEnvelope, announceFailure, removeKey: adapter.removeKey, fallback, cloneJson, emit,
  })
  state.memory = latest()
  return {
    read() {
      if (state.unsaved) return cloneJson(state.memory)
      return latest()
    },
    update(mutator) {
      return commitLocalViewStoreUpdate({
        state, mutator, fallback, parseData: options.parseData, cloneJson,
        readRecord: adapter.readRecord, writeRecord: adapter.writeRecord,
        removeKey: adapter.removeKey, announceFailure, emit,
      })
    },
    reset() {
      adapter.removeKey()
      state.memory = fallback()
      state.revision = 0
      state.unsaved = false
      emit(state.memory)
      return cloneJson(state.memory)
    },
    subscribe(listener) {
      listeners.add(listener)
      subscription.attach()
      return () => {
        listeners.delete(listener)
        if (listeners.size === 0) subscription.detach()
      }
    },
  }
}
