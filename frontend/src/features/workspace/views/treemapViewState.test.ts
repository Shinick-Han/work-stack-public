import { afterEach, describe, expect, it } from "vitest"

import { createLocalViewStore, localViewStorageKey } from "./localViewState"
import {
  EMPTY_TREEMAP_LOCAL_VIEW,
  TREEMAP_IDS_PER_SCOPE_CAP,
  TREEMAP_SCOPE_CAP,
  TREEMAP_TOTAL_IDS_CAP,
  boundTreemapOrders,
  idsForScope,
  legacyObjectiveScope,
  outcomeLeafScope,
  parseTreemapLocalViewData,
  pruneTreemapLocalViewData,
  rootScope,
  upsertTreemapScope,
} from "./treemapViewState"

const WORKSPACE = "ws-treemap-1"
const KEY = localViewStorageKey(WORKSPACE, "treemap")

afterEach(() => {
  window.localStorage.clear()
})

describe("parseTreemapLocalViewData", () => {
  it("S1 accepts a bounded payload and rejects unknown keys, duplicates, and oversize records", () => {
    expect(parseTreemapLocalViewData({
      orders: [{ scope: rootScope("legacy"), ids: ["O-1", "O-2"], touchedAt: 1 }],
    })).toEqual({
      orders: [{ scope: rootScope("legacy"), ids: ["O-1", "O-2"], touchedAt: 1 }],
    })
    expect(parseTreemapLocalViewData({
      orders: [{ scope: rootScope("legacy"), ids: ["O-1"], touchedAt: 1 }],
      extra: true,
    })).toBeNull()
    expect(parseTreemapLocalViewData({
      orders: [
        { scope: rootScope("legacy"), ids: ["O-1"], touchedAt: 1 },
        { scope: rootScope("legacy"), ids: ["O-2"], touchedAt: 2 },
      ],
    })).toBeNull()
    expect(parseTreemapLocalViewData({
      orders: [{ scope: "not-a-scope", ids: ["O-1"], touchedAt: 1 }],
    })).toBeNull()
    expect(parseTreemapLocalViewData({
      orders: [{ scope: rootScope("legacy"), ids: ["O-1", "O-1"], touchedAt: 1 }],
    })).toBeNull()
    expect(parseTreemapLocalViewData({
      orders: [{ scope: rootScope("legacy"), ids: ["O-1"], touchedAt: Number.POSITIVE_INFINITY }],
    })).toBeNull()
    expect(parseTreemapLocalViewData({
      orders: [{ scope: rootScope("legacy"), ids: ["O-1"], touchedAt: 1, extra: true }],
    })).toBeNull()
    expect(parseTreemapLocalViewData({
      orders: [{
        scope: legacyObjectiveScope("O-1"),
        ids: Array.from({ length: TREEMAP_IDS_PER_SCOPE_CAP + 1 }, (_, index) => `t-${index}`),
        touchedAt: 1,
      }],
    })).toBeNull()
    expect(parseTreemapLocalViewData({
      orders: Array.from({ length: TREEMAP_SCOPE_CAP + 1 }, (_, index) => ({
        scope: legacyObjectiveScope(`O-${index}`),
        ids: [`t-${index}`],
        touchedAt: index,
      })),
    })).toBeNull()
  })
})

describe("prune, isolate, and cap treemap scopes", () => {
  it("prunes proven deletions, keeps filtered catalog IDs, and leaves the other mode alone", () => {
    const data = {
      orders: [
        { scope: rootScope("legacy"), ids: ["O-1", "gone", "O-2"], touchedAt: 3 },
        { scope: legacyObjectiveScope("O-1"), ids: ["T-1", "T-hidden", "deleted"], touchedAt: 2 },
        { scope: outcomeLeafScope("g-1"), ids: ["T-out"], touchedAt: 9 },
        { scope: rootScope("outcome"), ids: ["none", "multiple"], touchedAt: 8 },
      ],
    }
    const catalog = new Map<string, string[]>([
      [rootScope("legacy"), ["O-1", "O-2"]],
      [legacyObjectiveScope("O-1"), ["T-1", "T-hidden"]],
    ])
    const pruned = pruneTreemapLocalViewData(data, catalog, "legacy")
    expect(idsForScope(pruned, rootScope("legacy"))).toEqual(["O-1", "O-2"])
    expect(idsForScope(pruned, legacyObjectiveScope("O-1"))).toEqual(["T-1", "T-hidden"])
    expect(idsForScope(pruned, outcomeLeafScope("g-1"))).toEqual(["T-out"])
    expect(idsForScope(pruned, rootScope("outcome"))).toEqual(["none", "multiple"])
  })

  it("evicts least-recent scopes when bounding writes", () => {
    const overflow = Array.from({ length: TREEMAP_SCOPE_CAP + 2 }, (_, index) => ({
      scope: legacyObjectiveScope(`O-${index}`),
      ids: [`t-${index}`],
      touchedAt: index,
    }))
    const bounded = boundTreemapOrders(overflow)
    expect(bounded.length).toBe(TREEMAP_SCOPE_CAP)
    expect(bounded.some((item) => item.scope === legacyObjectiveScope("O-0"))).toBe(false)
    expect(bounded.some((item) => item.scope === legacyObjectiveScope(`O-${TREEMAP_SCOPE_CAP + 1}`))).toBe(true)

    const heavy = [{
      scope: rootScope("legacy"),
      ids: Array.from({ length: TREEMAP_TOTAL_IDS_CAP }, (_, index) => `n-${index}`),
      touchedAt: 1,
    }, {
      scope: legacyObjectiveScope("fresh"),
      ids: ["fresh"],
      touchedAt: 2,
    }]
    const capped = boundTreemapOrders(heavy)
    expect(capped.some((item) => item.scope === legacyObjectiveScope("fresh"))).toBe(true)
    expect(capped.reduce((sum, item) => sum + item.ids.length, 0)).toBeLessThanOrEqual(TREEMAP_TOTAL_IDS_CAP)
  })

  it("rejects an upsert into the opposite mode and writes a legal none/multiple root", () => {
    const catalog = new Map<string, string[]>([[rootScope("outcome"), ["none", "multiple"]]])
    const current = EMPTY_TREEMAP_LOCAL_VIEW
    expect(upsertTreemapScope(current, rootScope("legacy"), ["none"], catalog, "outcome")).toBeNull()
    const written = upsertTreemapScope(
      current,
      rootScope("outcome"),
      ["multiple", "none"],
      catalog,
      "outcome",
      4,
    )
    expect(idsForScope(written ?? current, rootScope("outcome"))).toEqual(["multiple", "none"])
  })
})

describe("treemap store integration", () => {
  it("round-trips through the shared adapter on the treemap key", () => {
    const adapter = createLocalViewStore({
      workspaceId: WORKSPACE,
      view: "treemap",
      defaultData: EMPTY_TREEMAP_LOCAL_VIEW,
      parseData: parseTreemapLocalViewData,
    })
    const written = adapter.update(() => ({
      orders: [{ scope: rootScope("legacy"), ids: ["O-1"], touchedAt: 3 }],
    }))
    expect(adapter.read()).toEqual(written)
    expect(JSON.parse(window.localStorage.getItem(KEY) as string).view).toBe("treemap")
    adapter.reset()
    expect(window.localStorage.getItem(KEY)).toBeNull()
    expect(window.localStorage.getItem(localViewStorageKey(WORKSPACE, "graph"))).toBeNull()
  })
})
