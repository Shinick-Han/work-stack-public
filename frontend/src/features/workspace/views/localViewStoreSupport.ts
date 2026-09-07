/**
 * Internals for `createLocalViewStore`: envelope codec, localStorage adapter,
 * and `storage` event subscription lifecycle. Kept adjacent so the public
 * store factory stays under the function-length gate without import cycles.
 */

const ENVELOPE_KEYS = [
  "data",
  "revision",
  "schemaVersion",
  "view",
  "workspaceId",
  "writerId",
  "writtenAt",
] as const

export type ParsedLocalViewRecord<D> = { revision: number; data: D }

export type LocalViewStoreState<D> = {
  memory: D
  revision: number
  unsaved: boolean
}

export type LocalViewEnvelopeCodec<D> = {
  workspaceId: string
  view: string
  schemaVersion: number
  recordLimit: number
  writerIdLimit: number
  exactPlainObject: (
    value: unknown,
    keys: readonly string[],
  ) => Record<string, unknown> | null
  isValidWorkspaceId: (value: unknown) => boolean
  parseData: (value: unknown) => D | null
}

function isSafeIntegerRevision(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= Number.MAX_SAFE_INTEGER
}

function isEpochMillis(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
}

function isWriterId(value: unknown, limit: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= limit
}

export function parseLocalViewEnvelope<D>(
  raw: string,
  codec: LocalViewEnvelopeCodec<D>,
): ParsedLocalViewRecord<D> | null {
  if (raw.length > codec.recordLimit) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  const record = codec.exactPlainObject(parsed, ENVELOPE_KEYS)
  if (!record) return null
  if (record.schemaVersion !== codec.schemaVersion) return null
  if (record.workspaceId !== codec.workspaceId || !codec.isValidWorkspaceId(record.workspaceId)) return null
  if (record.view !== codec.view) return null
  if (!isSafeIntegerRevision(record.revision)) return null
  if (!isEpochMillis(record.writtenAt)) return null
  if (!isWriterId(record.writerId, codec.writerIdLimit)) return null
  const data = codec.parseData(record.data)
  if (data === null) return null
  return { revision: record.revision, data }
}

export function encodeLocalViewEnvelope(envelope: unknown, recordLimit: number): string | null {
  let serialized: string
  try {
    serialized = JSON.stringify(envelope)
  } catch {
    return null
  }
  if (serialized.length > recordLimit) return null
  return serialized
}

export function localViewBrowserStorage(): Storage | null {
  try {
    if (typeof window === "undefined" || !window.localStorage) return null
    return window.localStorage
  } catch {
    return null
  }
}

export function createLocalViewStorageAdapter<D>(options: {
  key: string | null
  recordLimit: number
  parseEnvelope: (raw: string) => ParsedLocalViewRecord<D> | null
  announceFailure: () => void
  schemaVersion: number
  workspaceId: string
  view: string
  writerId: string
}) {
  const removeKey = () => {
    if (!options.key) return
    const local = localViewBrowserStorage()
    if (!local) return
    try {
      local.removeItem(options.key)
    } catch {
      options.announceFailure()
    }
  }

  const readRecord = (): ParsedLocalViewRecord<D> | null => {
    if (!options.key) return null
    const local = localViewBrowserStorage()
    if (!local) {
      options.announceFailure()
      return null
    }
    let raw: string | null
    try {
      raw = local.getItem(options.key)
    } catch {
      options.announceFailure()
      return null
    }
    if (raw === null) return null
    const parsed = options.parseEnvelope(raw)
    if (!parsed) {
      options.announceFailure()
      removeKey()
      return null
    }
    return parsed
  }

  const writeRecord = (nextRevision: number, data: D): boolean => {
    if (!options.key) return false
    const local = localViewBrowserStorage()
    if (!local) {
      options.announceFailure()
      return false
    }
    const serialized = encodeLocalViewEnvelope({
      schemaVersion: options.schemaVersion,
      workspaceId: options.workspaceId,
      view: options.view,
      revision: nextRevision,
      writtenAt: Date.now(),
      writerId: options.writerId,
      data,
    }, options.recordLimit)
    if (serialized === null) {
      options.announceFailure()
      return false
    }
    try {
      local.setItem(options.key, serialized)
      return true
    } catch {
      options.announceFailure()
      return false
    }
  }

  return { removeKey, readRecord, writeRecord }
}

export function createLocalViewStorageSubscription<D>(options: {
  key: string | null
  state: LocalViewStoreState<D>
  parseEnvelope: (raw: string) => ParsedLocalViewRecord<D> | null
  announceFailure: () => void
  removeKey: () => void
  fallback: () => D
  cloneJson: <T>(value: T) => T
  emit: (data: D) => void
}) {
  let listening = false

  const onStorage = (event: StorageEvent) => {
    if (!options.key) return
    if (event.key !== options.key) return
    if (event.storageArea && localViewBrowserStorage() && event.storageArea !== localViewBrowserStorage()) return
    if (event.newValue === null || event.newValue === "") {
      options.state.memory = options.fallback()
      options.state.revision = 0
      options.state.unsaved = false
      options.emit(options.state.memory)
      return
    }
    const parsed = options.parseEnvelope(event.newValue)
    if (!parsed) {
      options.announceFailure()
      options.removeKey()
      options.state.memory = options.fallback()
      options.state.revision = 0
      options.state.unsaved = false
      options.emit(options.state.memory)
      return
    }
    options.state.memory = options.cloneJson(parsed.data)
    options.state.revision = parsed.revision
    options.state.unsaved = false
    options.emit(options.state.memory)
  }

  return {
    attach() {
      if (listening || typeof window === "undefined") return
      window.addEventListener("storage", onStorage)
      listening = true
    },
    detach() {
      if (!listening || typeof window === "undefined") return
      window.removeEventListener("storage", onStorage)
      listening = false
    },
  }
}

export function readLocalViewStoreLatest<D>(options: {
  state: LocalViewStoreState<D>
  readRecord: () => ParsedLocalViewRecord<D> | null
  fallback: () => D
  cloneJson: <T>(value: T) => T
}): D {
  const record = options.readRecord()
  if (record) {
    options.state.memory = options.cloneJson(record.data)
    options.state.revision = record.revision
    options.state.unsaved = false
    return options.cloneJson(options.state.memory)
  }
  if (options.state.unsaved || options.state.revision > 0) return options.cloneJson(options.state.memory)
  options.state.memory = options.fallback()
  options.state.revision = 0
  return options.cloneJson(options.state.memory)
}

export function commitLocalViewStoreUpdate<D>(options: {
  state: LocalViewStoreState<D>
  mutator: (current: D) => D
  fallback: () => D
  parseData: (value: unknown) => D | null
  cloneJson: <T>(value: T) => T
  readRecord: () => ParsedLocalViewRecord<D> | null
  writeRecord: (nextRevision: number, data: D) => boolean
  removeKey: () => void
  announceFailure: () => void
  emit: (data: D) => void
}): D {
  const { state, cloneJson } = options
  const record = state.unsaved ? null : options.readRecord()
  const latest = record
    ? cloneJson(record.data)
    : (state.revision > 0 || state.unsaved ? cloneJson(state.memory) : options.fallback())
  if (record) state.revision = record.revision
  const proposed = options.mutator(latest)
  const data = options.parseData(cloneJson(proposed))
  if (data === null) {
    options.announceFailure()
    return cloneJson(state.memory)
  }
  const nextRevision = state.revision >= Number.MAX_SAFE_INTEGER ? 1 : state.revision + 1
  if (state.revision >= Number.MAX_SAFE_INTEGER) options.removeKey()
  state.memory = cloneJson(data)
  state.revision = nextRevision
  state.unsaved = !options.writeRecord(nextRevision, state.memory)
  options.emit(state.memory)
  return cloneJson(state.memory)
}
