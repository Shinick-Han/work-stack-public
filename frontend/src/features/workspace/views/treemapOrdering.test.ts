import { applyVisibleReorder } from "./localViewState"
import {
  applyPreviewToGroups,
  applyStoredTreemapOrders,
  collectTreemapCatalog,
  formatTreemapPosition,
  isNavigatorGroup,
  keyboardTargetIndex,
  orderRootGroups,
  previewMove,
  splitLegacyReservedGroups,
  visibleIdsForScope,
  type TreemapOrderNode,
} from "./treemapOrdering"
import {
  EMPTY_TREEMAP_LOCAL_VIEW,
  LEGACY_MULTIPLE_SCOPE,
  LEGACY_OPERATIONS_SCOPE,
  commitVisibleTreemapReorder,
  idsForScope,
  legacyLeafScope,
  legacyObjectiveScope,
  outcomeLeafScope,
  rootScope,
} from "./treemapViewState"

function objective(id: string, tasks: string[]): TreemapOrderNode {
  return {
    name: id,
    objectiveId: id,
    children: tasks.map((taskId) => ({ name: taskId, taskId })),
  }
}

function outcomeObjective(
  id: string,
  groups: { key: string; nodeKind?: string; tasks: string[] }[],
): TreemapOrderNode {
  return {
    name: id,
    nodeKind: "objective",
    objectiveId: id,
    groupKey: `obj:${id}`,
    children: groups.map((group) => ({
      name: group.key,
      nodeKind: group.nodeKind ?? "key-result",
      groupKey: group.key,
      objectiveId: id,
      children: group.tasks.map((taskId) => ({ name: taskId, taskId, nodeKind: "task" })),
    })),
  }
}

describe("treemap sibling ordering", () => {
  it("TR1 reorders real root Objectives and terminal leaves without moving hierarchy", () => {
    const groups = [
      objective("O-1", ["T-1", "T-2"]),
      objective("O-2", ["T-3"]),
      { name: "Multiple objectives", objectiveId: "multiple", children: [{ name: "T-m", taskId: "T-m" }] },
      { name: "Unaligned / Operations", objectiveId: "none", children: [{ name: "T-ops", taskId: "T-ops" }] },
    ]
    const data = {
      orders: [
        { scope: rootScope("legacy"), ids: ["O-2", "O-1"], touchedAt: 1 },
        { scope: legacyObjectiveScope("O-1"), ids: ["T-2", "T-1"], touchedAt: 1 },
      ],
    }
    const ordered = applyStoredTreemapOrders(groups, data, "legacy")
    expect(ordered.map((group) => group.objectiveId)).toEqual(["O-2", "O-1", "multiple", "none"])
    expect(ordered[1]?.children?.map((leaf) => leaf.taskId)).toEqual(["T-2", "T-1"])
    expect(ordered[0]?.children?.map((leaf) => leaf.taskId)).toEqual(["T-3"])
    expect(ordered[2]?.children?.map((leaf) => leaf.taskId)).toEqual(["T-m"])
  })

  it("TR2 keeps a multi-Objective Task as one leaf inside its derived bucket", () => {
    const groups = [
      objective("O-1", ["T-1"]),
      { name: "Multiple objectives", objectiveId: "multiple", children: [{ name: "T-multi", taskId: "T-multi" }] },
    ]
    const moved = applyStoredTreemapOrders(groups, {
      orders: [{ scope: LEGACY_MULTIPLE_SCOPE, ids: ["T-multi"], touchedAt: 1 }],
    }, "legacy")
    const multiple = moved.filter((group) => group.objectiveId === "multiple")
    expect(multiple).toHaveLength(1)
    expect(multiple[0]?.children?.map((leaf) => leaf.taskId)).toEqual(["T-multi"])
    expect(moved.flatMap((group) => group.children ?? []).filter((leaf) => leaf.taskId === "T-multi")).toHaveLength(1)
  })

  it("F1 projects visible slots, appends newly seen IDs, and does not invent hierarchy", () => {
    const stored = ["T-1", "T-hidden", "T-2"]
    const canonical = ["T-1", "T-hidden", "T-2", "T-new"]
    const visible = ["T-1", "T-2"]
    expect(applyVisibleReorder(stored, canonical, visible, ["T-2", "T-1"])).toEqual([
      "T-2", "T-hidden", "T-1", "T-new",
    ])
    const groups = [objective("O-1", ["T-2", "T-1"])]
    const ordered = applyStoredTreemapOrders(groups, {
      orders: [{ scope: legacyObjectiveScope("O-1"), ids: ["T-1", "T-hidden", "T-2"], touchedAt: 1 }],
    }, "legacy")
    expect(ordered[0]?.children?.map((leaf) => leaf.taskId)).toEqual(["T-1", "T-2"])
  })

  it("isolates legal Objective IDs named none/multiple from Operations", () => {
    const groups = [
      { name: "none", nodeKind: "objective", objectiveId: "none", children: [] },
      { name: "multiple", nodeKind: "objective", objectiveId: "multiple", children: [] },
      { name: "Operations", nodeKind: "objective", objectiveId: null, groupKey: "ops", children: [] },
    ]
    expect(groups.filter((group) => isNavigatorGroup(group, "outcome")).map((group) => group.objectiveId))
      .toEqual(["none", "multiple"])
    const ordered = orderRootGroups(groups, ["multiple", "none"], "outcome")
    expect(ordered.map((group) => group.objectiveId)).toEqual(["multiple", "none", null])
  })

  it("gives legal none/multiple Objective IDs their own legacy scopes", () => {
    const groups = [
      {
        name: "Unaligned / Operations",
        objectiveId: "none",
        children: [
          { taskId: "T-n", objectiveIds: ["none"] },
          { taskId: "T-ops", objectiveIds: [] },
        ],
      },
      {
        name: "Multiple objectives",
        objectiveId: "multiple",
        children: [
          { taskId: "T-named", objectiveIds: ["multiple"] },
          { taskId: "T-multi", objectiveIds: ["O-1", "O-2"] },
        ],
      },
    ]
    const split = splitLegacyReservedGroups(groups)
    expect(split.map((group) => [group.objectiveId, group.syntheticBucket, group.children?.map((leaf) => leaf.taskId)]))
      .toEqual([
        ["none", undefined, ["T-n"]],
        ["none", "operations", ["T-ops"]],
        ["multiple", undefined, ["T-named"]],
        ["multiple", "multiple", ["T-multi"]],
      ])
    const catalog = collectTreemapCatalog(groups, "legacy")
    expect(catalog.get(legacyObjectiveScope("none"))).toEqual(["T-n"])
    expect(catalog.get(LEGACY_OPERATIONS_SCOPE)).toEqual(["T-ops"])
    expect(catalog.get(legacyObjectiveScope("multiple"))).toEqual(["T-named"])
    expect(catalog.get(LEGACY_MULTIPLE_SCOPE)).toEqual(["T-multi"])
    expect(catalog.get(rootScope("legacy"))).toEqual(["none", "multiple"])
    expect(isNavigatorGroup(split[1]!, "legacy")).toBe(false)
    expect(isNavigatorGroup(split[0]!, "legacy")).toBe(true)
  })

  it("keeps Operations after ordered real Objectives in legacy root order", () => {
    const groups = [
      objective("O-1", ["T-1"]),
      { name: "Unaligned / Operations", objectiveId: "none", children: [{ taskId: "T-ops" }] },
    ]
    expect(legacyLeafScope({ name: "Unaligned / Operations", objectiveId: "none" })).toBe(LEGACY_OPERATIONS_SCOPE)
    expect(visibleIdsForScope(groups, rootScope("legacy"), "legacy")).toEqual(["O-1"])
    expect(orderRootGroups(groups, ["O-1"], "legacy").map((group) => group.objectiveId)).toEqual(["O-1", "none"])
  })

  it("uses distinct outcome scopes for duplicate raw KR IDs in different Objectives", () => {
    const left = outcomeLeafScope("ws|W1|kr|O-A|K1")
    const right = outcomeLeafScope("ws|W1|kr|O-B|K1")
    expect(left).not.toBe(right)
    const groups = [
      outcomeObjective("O-A", [{ key: "ws|W1|kr|O-A|K1", tasks: ["T-a2", "T-a1"] }]),
      outcomeObjective("O-B", [{ key: "ws|W1|kr|O-B|K1", tasks: ["T-b1"] }]),
    ]
    const data = {
      orders: [
        { scope: left, ids: ["T-a1", "T-a2"], touchedAt: 1 },
        { scope: right, ids: ["T-b1"], touchedAt: 1 },
      ],
    }
    const ordered = applyStoredTreemapOrders(groups, data, "outcome")
    expect(ordered[0]?.children?.[0]?.children?.map((leaf) => leaf.taskId)).toEqual(["T-a1", "T-a2"])
    expect(ordered[1]?.children?.[0]?.children?.map((leaf) => leaf.taskId)).toEqual(["T-b1"])
    const catalog = collectTreemapCatalog(groups, "outcome")
    expect(catalog.get(left)).toEqual(["T-a2", "T-a1"])
    expect(catalog.get(right)).toEqual(["T-b1"])
  })

  it("does not leak legacy order into outcome scopes", () => {
    const groups = [
      outcomeObjective("O-1", [{ key: "g-1", tasks: ["T-1", "T-2"] }]),
      objective("O-1", ["T-9", "T-8"]),
    ]
    const data = {
      orders: [
        { scope: rootScope("legacy"), ids: ["O-9"], touchedAt: 1 },
        { scope: legacyObjectiveScope("O-1"), ids: ["T-8", "T-9"], touchedAt: 1 },
        { scope: rootScope("outcome"), ids: ["O-1"], touchedAt: 1 },
        { scope: outcomeLeafScope("g-1"), ids: ["T-2", "T-1"], touchedAt: 1 },
      ],
    }
    const outcome = applyStoredTreemapOrders([groups[0]!], data, "outcome")
    expect(outcome[0]?.children?.[0]?.children?.map((leaf) => leaf.taskId)).toEqual(["T-2", "T-1"])
    const legacy = applyStoredTreemapOrders([groups[1]!], data, "legacy")
    expect(legacy[0]?.children?.map((leaf) => leaf.taskId)).toEqual(["T-8", "T-9"])
    expect(idsForScope(data, rootScope("legacy"))).not.toEqual(idsForScope(data, rootScope("outcome")))
  })

  it("previews keyboard movement and names 1-based positions", () => {
    expect(previewMove(["A", "B", "C"], 0, 2)).toEqual(["B", "C", "A"])
    expect(keyboardTargetIndex(1, 3, "Home")).toBe(0)
    expect(keyboardTargetIndex(1, 3, "End")).toBe(2)
    expect(keyboardTargetIndex(1, 3, "ArrowLeft")).toBe(0)
    expect(formatTreemapPosition(1, 4)).toBe("2 of 4")
    const groups = [objective("O-1", ["T-1"]), objective("O-2", ["T-2"])]
    const previewed = applyPreviewToGroups(groups, rootScope("legacy"), ["O-2", "O-1"], "legacy")
    expect(previewed.map((group) => group.objectiveId)).toEqual(["O-2", "O-1"])
  })
})

describe("visible reorder commit against a catalog", () => {
  it("replaces only visible slots for the affected scope", () => {
    const catalog = new Map<string, string[]>([
      [legacyObjectiveScope("O-1"), ["T-1", "T-hidden", "T-2"]],
    ])
    const current = {
      orders: [{ scope: legacyObjectiveScope("O-1"), ids: ["T-1", "T-hidden", "T-2"], touchedAt: 1 }],
    }
    const next = commitVisibleTreemapReorder(
      current,
      legacyObjectiveScope("O-1"),
      ["T-1", "T-hidden", "T-2"],
      ["T-1", "T-2"],
      ["T-2", "T-1"],
      catalog,
      "legacy",
      9,
    )
    expect(idsForScope(next ?? EMPTY_TREEMAP_LOCAL_VIEW, legacyObjectiveScope("O-1")))
      .toEqual(["T-2", "T-hidden", "T-1"])
  })
})
