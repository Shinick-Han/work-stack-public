import { afterEach, describe, expect, it, vi } from "vitest"

import {
  applyVisibleReorder,
  createExternalUpdateGate,
  createLocalViewStore,
  exactPlainObject,
  isPayloadId,
  isPlainObject,
  LAYOUT_SAVE_FAILURE_MESSAGE,
  LOCAL_VIEW_RECORD_LIMIT,
  LOCAL_VIEW_WORKSPACE_ID_LIMIT,
  localViewStorageKey,
  normalizeStoredOrder,
  uniqueStringArray,
} from "./localViewState"

type Sample = { ids: string[]; n: number }

const WORKSPACE = "ws-local-1"
const OTHER = "ws-local-2"
const KEY = localViewStorageKey(WORKSPACE, "graph")
const TABLE_KEY = localViewStorageKey(WORKSPACE, "table")

function parseSample(value: unknown): Sample | null {
  const record = exactPlainObject(value, ["ids", "n"])
  if (!record) return null
  const ids = uniqueStringArray(record.ids)
  if (!ids) return null
  if (typeof record.n !== "number" || !Number.isFinite(record.n)) return null
  return { ids, n: record.n }
}

const DEFAULT: Sample = { ids: [], n: 0 }

function store(onPersistFailure?: () => void, workspaceId = WORKSPACE) {
  return createLocalViewStore({
    workspaceId,
    view: "graph",
    defaultData: DEFAULT,
    parseData: parseSample,
    onPersistFailure,
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe("storage key identity", () => {
  it("isolates workspace and view, including encoded identities", () => {
    const exotic = "ws/뷰:1 2"
    expect(localViewStorageKey(exotic, "graph")).toBe(
      `workstack:local-view:v1:${encodeURIComponent(exotic)}:graph`,
    )
    expect(localViewStorageKey(WORKSPACE, "graph")).not.toBe(localViewStorageKey(WORKSPACE, "table"))
    expect(localViewStorageKey(WORKSPACE, "graph")).not.toBe(localViewStorageKey(OTHER, "graph"))
  })
})

describe("S1 fail-closed envelope and payload bounds", () => {
  it("accepts a valid write and rereads the same payload", () => {
    const adapter = store()
    const written = adapter.update(() => ({ ids: ["T-1", "T-2"], n: 3 }))
    expect(written).toEqual({ ids: ["T-1", "T-2"], n: 3 })
    expect(adapter.read()).toEqual(written)
    const raw = JSON.parse(window.localStorage.getItem(KEY) as string)
    expect(raw).toMatchObject({
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: 1,
      writerId: expect.any(String),
      data: written,
    })
    expect(raw.writerId.length).toBeGreaterThan(0)
    expect(raw.writerId.length).toBeLessThanOrEqual(64)
    expect(Number.isFinite(raw.writtenAt) && raw.writtenAt >= 0).toBe(true)
  })

  it("removes wrong workspace, wrong view, unknown keys, and malformed JSON", () => {
    const adapter = store()
    window.localStorage.setItem(KEY, JSON.stringify({
      schemaVersion: 1,
      workspaceId: OTHER,
      view: "graph",
      revision: 1,
      writtenAt: 1,
      writerId: "tab",
      data: { ids: ["T-1"], n: 1 },
    }))
    expect(adapter.read()).toEqual(DEFAULT)
    expect(window.localStorage.getItem(KEY)).toBeNull()

    window.localStorage.setItem(KEY, JSON.stringify({
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "table",
      revision: 1,
      writtenAt: 1,
      writerId: "tab",
      data: { ids: ["T-1"], n: 1 },
    }))
    expect(adapter.read()).toEqual(DEFAULT)
    expect(window.localStorage.getItem(KEY)).toBeNull()

    window.localStorage.setItem(KEY, JSON.stringify({
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: 1,
      writtenAt: 1,
      writerId: "tab",
      data: { ids: ["T-1"], n: 1 },
      extra: true,
    }))
    expect(adapter.read()).toEqual(DEFAULT)
    expect(window.localStorage.getItem(KEY)).toBeNull()

    window.localStorage.setItem(KEY, "{not json")
    expect(adapter.read()).toEqual(DEFAULT)
    expect(window.localStorage.getItem(KEY)).toBeNull()
  })

  it("rejects duplicates, nonfinite numbers, prototype-bearing objects, and oversize IDs", () => {
    expect(uniqueStringArray(["A", "A"])).toBeNull()
    expect(uniqueStringArray(["A", "B"])).toEqual(["A", "B"])
    expect(isPayloadId("x".repeat(256))).toBe(true)
    expect(isPayloadId("x".repeat(257))).toBe(false)
    expect(isPayloadId("")).toBe(false)
    expect(isPlainObject(Object.create({ n: 1 }))).toBe(false)
    expect(isPlainObject({ n: 1 })).toBe(true)

    const adapter = store()
    window.localStorage.setItem(KEY, JSON.stringify({
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: 1,
      writtenAt: 1,
      writerId: "tab",
      data: { ids: ["T-1", "T-1"], n: 1 },
    }))
    expect(adapter.read()).toEqual(DEFAULT)
    expect(window.localStorage.getItem(KEY)).toBeNull()

    window.localStorage.setItem(KEY, JSON.stringify({
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: 1,
      writtenAt: Number.NaN,
      writerId: "tab",
      data: { ids: ["T-1"], n: 1 },
    }))
    expect(adapter.read()).toEqual(DEFAULT)
  })

  it("does not persist oversize records or invalid workspace identities", () => {
    const fail = vi.fn()
    const adapter = store(fail)
    const huge = adapter.update(() => ({ ids: ["T-1"], n: 0, } as Sample))
    expect(huge).toEqual({ ids: ["T-1"], n: 0 })

    const oversize = adapter.update(() => ({ ids: ["T-1"], n: 1 }))
    expect(oversize.ids).toEqual(["T-1"])
    const blobStore = createLocalViewStore({
      workspaceId: WORKSPACE,
      view: "treemap",
      defaultData: { blob: "" },
      parseData: (value) => {
        const record = exactPlainObject(value, ["blob"])
        return record && typeof record.blob === "string" ? { blob: record.blob } : null
      },
      onPersistFailure: fail,
    })
    const tooBig = blobStore.update(() => ({ blob: "x".repeat(LOCAL_VIEW_RECORD_LIMIT) }))
    expect(tooBig.blob.length).toBe(LOCAL_VIEW_RECORD_LIMIT)
    expect(window.localStorage.getItem(localViewStorageKey(WORKSPACE, "treemap"))).toBeNull()
    expect(fail).toHaveBeenCalledTimes(1)

    const invalidId = createLocalViewStore({
      workspaceId: "w".repeat(LOCAL_VIEW_WORKSPACE_ID_LIMIT + 1),
      view: "graph",
      defaultData: DEFAULT,
      parseData: parseSample,
    })
    invalidId.update(() => ({ ids: ["T-1"], n: 1 }))
    expect(window.localStorage.getItem(
      localViewStorageKey("w".repeat(LOCAL_VIEW_WORKSPACE_ID_LIMIT + 1), "graph"),
    )).toBeNull()
    expect(window.localStorage.getItem(KEY)).not.toBeNull()
  })

  it("falls back in memory when storage is missing or throws, and announces once", () => {
    const fail = vi.fn()
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError")
    })
    const adapter = store(fail)
    const first = adapter.update(() => ({ ids: ["T-9"], n: 9 }))
    const second = adapter.update((current) => ({ ...current, n: 10 }))
    expect(first).toEqual({ ids: ["T-9"], n: 9 })
    expect(second).toEqual({ ids: ["T-9"], n: 10 })
    expect(adapter.read()).toEqual(second)
    expect(fail).toHaveBeenCalledTimes(1)
    expect(LAYOUT_SAVE_FAILURE_MESSAGE.length).toBeGreaterThan(0)
  })

  it("rejects an invalid mutator result without clobbering the last good record", () => {
    const fail = vi.fn()
    const adapter = store(fail)
    adapter.update(() => ({ ids: ["T-1"], n: 1 }))
    const kept = adapter.update(() => ({ ids: ["T-1", "T-1"], n: 2 }))
    expect(kept).toEqual({ ids: ["T-1"], n: 1 })
    expect(adapter.read()).toEqual({ ids: ["T-1"], n: 1 })
    expect(fail).toHaveBeenCalledTimes(1)
  })

  it("announces once for corrupt, oversized, disabled, and security-blocked storage", () => {
    const corrupt = vi.fn()
    window.localStorage.setItem(KEY, "{not json")
    const corruptStore = store(corrupt)
    expect(corruptStore.read()).toEqual(DEFAULT)
    expect(window.localStorage.getItem(KEY)).toBeNull()
    corruptStore.read()
    expect(corrupt).toHaveBeenCalledTimes(1)

    const oversized = vi.fn()
    window.localStorage.setItem(KEY, "x".repeat(LOCAL_VIEW_RECORD_LIMIT + 8))
    const oversizedStore = store(oversized)
    expect(oversizedStore.read()).toEqual(DEFAULT)
    oversizedStore.read()
    expect(oversized).toHaveBeenCalledTimes(1)

    const disabled = vi.fn()
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError")
    })
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError")
    })
    const blocked = store(disabled)
    const kept = blocked.update(() => ({ ids: ["T-live"], n: 4 }))
    expect(kept).toEqual({ ids: ["T-live"], n: 4 })
    expect(blocked.read()).toEqual(kept)
    blocked.update((current) => ({ ...current, n: 5 }))
    expect(blocked.read()).toEqual({ ids: ["T-live"], n: 5 })
    expect(disabled).toHaveBeenCalledTimes(1)
  })

  it("keeps the newest in-memory fallback after quota instead of rereading an older durable record", () => {
    const fail = vi.fn()
    const adapter = store(fail)
    adapter.update(() => ({ ids: ["old"], n: 1 }))
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).data).toEqual({ ids: ["old"], n: 1 })
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError")
    })
    const newest = adapter.update(() => ({ ids: ["new"], n: 2 }))
    const composed = adapter.update((current) => ({ ids: current.ids, n: current.n + 5 }))
    expect(newest).toEqual({ ids: ["new"], n: 2 })
    expect(composed).toEqual({ ids: ["new"], n: 7 })
    expect(adapter.read()).toEqual(composed)
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).data).toEqual({ ids: ["old"], n: 1 })
    expect(fail).toHaveBeenCalledTimes(1)
  })
})

describe("C1 cross-tab storage and gesture rebase", () => {
  it("replaces idle state from a matching storage event and treats removal as reset", () => {
    const adapter = store()
    adapter.update(() => ({ ids: ["T-1"], n: 1 }))
    const seen: Sample[] = []
    const stop = adapter.subscribe((data) => { seen.push(data) })

    const incoming = {
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: 4,
      writtenAt: 50,
      writerId: "other-tab",
      data: { ids: ["T-8"], n: 8 },
    }
    window.localStorage.setItem(KEY, JSON.stringify(incoming))
    window.dispatchEvent(new StorageEvent("storage", {
      key: KEY,
      newValue: JSON.stringify(incoming),
      storageArea: window.localStorage,
    }))
    expect(seen.at(-1)).toEqual({ ids: ["T-8"], n: 8 })
    expect(adapter.read()).toEqual({ ids: ["T-8"], n: 8 })

    window.localStorage.removeItem(KEY)
    window.dispatchEvent(new StorageEvent("storage", {
      key: KEY,
      newValue: null,
      storageArea: window.localStorage,
    }))
    expect(seen.at(-1)).toEqual(DEFAULT)

    window.dispatchEvent(new StorageEvent("storage", {
      key: TABLE_KEY,
      newValue: JSON.stringify({ ...incoming, view: "table" }),
      storageArea: window.localStorage,
    }))
    expect(seen.filter((item) => item.n === 8)).toHaveLength(1)
    stop()
  })

  it("buffers external state during a gesture, restores it on cancel, and rebases on commit", () => {
    const adapter = store()
    let ui = adapter.read()
    const gate = createExternalUpdateGate<Sample>((data) => { ui = data })
    adapter.subscribe((data) => { gate.onExternal(data) })

    adapter.update(() => ({ ids: ["A", "B"], n: 1 }))
    ui = adapter.read()
    gate.beginGesture()

    const foreign = {
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: 9,
      writtenAt: 9,
      writerId: "peer",
      data: { ids: ["A", "C"], n: 9 },
    }
    window.localStorage.setItem(KEY, JSON.stringify(foreign))
    window.dispatchEvent(new StorageEvent("storage", {
      key: KEY,
      newValue: JSON.stringify(foreign),
      storageArea: window.localStorage,
    }))
    expect(ui).toEqual({ ids: ["A", "B"], n: 1 })
    expect(gate.buffered).toEqual({ ids: ["A", "C"], n: 9 })

    gate.cancelGesture()
    expect(ui).toEqual({ ids: ["A", "C"], n: 9 })

    ui = { ids: ["A", "B"], n: 1 }
    gate.beginGesture()
    window.dispatchEvent(new StorageEvent("storage", {
      key: KEY,
      newValue: JSON.stringify(foreign),
      storageArea: window.localStorage,
    }))
    gate.endGesture()
    const committed = adapter.update((current) => ({
      ids: [...current.ids.filter((id) => id !== "B"), "B"],
      n: current.n + 1,
    }))
    expect(committed.ids).toEqual(["A", "C", "B"])
    expect(committed.n).toBe(10)
  })

  it("drops storage listeners when the last subscriber unsubscribes", () => {
    const adapter = store()
    const listener = vi.fn()
    const stop = adapter.subscribe(listener)
    stop()
    window.dispatchEvent(new StorageEvent("storage", {
      key: KEY,
      newValue: JSON.stringify({
        schemaVersion: 1,
        workspaceId: WORKSPACE,
        view: "graph",
        revision: 2,
        writtenAt: 2,
        writerId: "peer",
        data: { ids: ["Z"], n: 2 },
      }),
      storageArea: window.localStorage,
    }))
    expect(listener).not.toHaveBeenCalled()
  })
})

describe("R1 per-view reset", () => {
  it("removes only its workspace/view key and restores defaults", () => {
    const graph = store()
    const table = createLocalViewStore({
      workspaceId: WORKSPACE,
      view: "table",
      defaultData: DEFAULT,
      parseData: parseSample,
    })
    const other = createLocalViewStore({
      workspaceId: OTHER,
      view: "graph",
      defaultData: DEFAULT,
      parseData: parseSample,
    })
    graph.update(() => ({ ids: ["G"], n: 1 }))
    table.update(() => ({ ids: ["T"], n: 2 }))
    other.update(() => ({ ids: ["O"], n: 3 }))

    expect(graph.reset()).toEqual(DEFAULT)
    expect(graph.read()).toEqual(DEFAULT)
    expect(window.localStorage.getItem(KEY)).toBeNull()
    expect(JSON.parse(window.localStorage.getItem(TABLE_KEY) as string).data).toEqual({ ids: ["T"], n: 2 })
    expect(JSON.parse(window.localStorage.getItem(localViewStorageKey(OTHER, "graph")) as string).data)
      .toEqual({ ids: ["O"], n: 3 })
  })

  it("wraps a saturated revision by replacing that one key at revision 1", () => {
    window.localStorage.setItem(KEY, JSON.stringify({
      schemaVersion: 1,
      workspaceId: WORKSPACE,
      view: "graph",
      revision: Number.MAX_SAFE_INTEGER,
      writtenAt: 1,
      writerId: "tab",
      data: { ids: ["T-1"], n: 1 },
    }))
    const adapter = store()
    adapter.update((current) => ({ ...current, n: 2 }))
    const raw = JSON.parse(window.localStorage.getItem(KEY) as string)
    expect(raw.revision).toBe(1)
    expect(raw.data).toEqual({ ids: ["T-1"], n: 2 })
  })
})

describe("shared visible-order rebase", () => {
  it("preserves hidden slots, appends unseen canonical IDs, and prunes only deletions", () => {
    const stored = ["A", "H", "B", "A", "GONE"]
    const canonical = ["A", "B", "C", "H"]
    expect(normalizeStoredOrder(stored, canonical)).toEqual(["A", "H", "B", "C"])
    expect(applyVisibleReorder(stored, canonical, ["A", "B"], ["B", "A"])).toEqual(["B", "H", "A", "C"])
  })
})
