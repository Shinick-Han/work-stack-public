import { describe, expect, it } from "vitest";

import {
  completedVisibilityScopeKey,
  normalizeDoneVisibility,
  projectCompletedTaskVisibility,
  REVEAL_DISPLAY_LIMIT,
  type CompletedVisibilityInput,
} from "./completedTaskVisibility";
import { filterWorkspaceTasks } from "./viewModels";
import type { WorkspaceTask } from "./types";

/**
 * Adversarial fixtures only; every assertion goes through the three public
 * exports and the real existing filter. No private helper is asserted on.
 */
function task(id: string, overrides: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return { id, title: `Task ${id}`, status: "open", priority: "P2", ...overrides };
}

function deepFreezeTasks(tasks: WorkspaceTask[]): readonly WorkspaceTask[] {
  for (const item of tasks) {
    if (item.dependencies) Object.freeze(item.dependencies);
    if (item.objective_ids) Object.freeze(item.objective_ids);
    if (item.tags) Object.freeze(item.tags);
    Object.freeze(item);
  }
  return Object.freeze(tasks);
}

function project(input: Partial<CompletedVisibilityInput> & { tasks: readonly WorkspaceTask[] }) {
  return projectCompletedTaskVisibility({
    filters: {},
    view: "graph",
    ...input,
  });
}

function ids(tasks: readonly WorkspaceTask[]): string[] {
  return tasks.map((item) => item.id);
}

/** The partition invariant must hold for every single case in this file. */
function expectExactPartition(projection: ReturnType<typeof project>) {
  const { counts } = projection;
  expect(counts.visible + counts.hiddenCompleted + counts.hiddenOther).toBe(
    counts.canonicalTotal,
  );
  expect(counts.visible).toBe(projection.visibleTasks.length);
  expect(counts.hiddenCompleted).toBe(projection.hiddenCompletedTaskIds.length);
  expect(counts.hiddenOther).toBe(projection.hiddenOtherTaskIds.length);
  const seen = new Set([
    ...ids(projection.visibleTasks),
    ...projection.hiddenCompletedTaskIds,
    ...projection.hiddenOtherTaskIds,
  ]);
  expect(seen.size).toBe(counts.canonicalTotal);
}

describe("V01 fresh default with Done, Open and Dropped", () => {
  const tasks = deepFreezeTasks([
    task("T-1"),
    task("T-2", { status: "done" }),
    task("T-3", { status: "dropped" }),
    task("T-4", { status: "started" }),
  ]);

  it("hides only Done and leaves canonical objects untouched", () => {
    const projection = project({ tasks });

    expect(ids(projection.visibleTasks)).toEqual(["T-1", "T-3", "T-4"]);
    expect(projection.hiddenCompletedTaskIds).toEqual(["T-2"]);
    expect(projection.hiddenOtherTaskIds).toEqual([]);
    expect(projection.emptyKind).toBe("none");
    expectExactPartition(projection);
  });

  it("keeps Dropped distinct from Done", () => {
    const projection = project({ tasks });
    expect(ids(projection.visibleTasks)).toContain("T-3");
    expect(projection.hiddenCompletedTaskIds).not.toContain("T-3");
  });
});

describe("V02 explicit Done status and explicit All both surface Done", () => {
  const tasks = deepFreezeTasks([task("T-1"), task("T-2", { status: "done" })]);

  it("shows Done under status=done even when doneVisibility is hide", () => {
    const projection = project({
      tasks,
      filters: { status: "done" },
      doneVisibility: "hide",
    });

    expect(ids(projection.visibleTasks)).toEqual(["T-2"]);
    expect(projection.hiddenOtherTaskIds).toEqual(["T-1"]);
    expectExactPartition(projection);
  });

  it("shows Done under status=all plus doneVisibility=show", () => {
    const projection = project({
      tasks,
      filters: { status: "all" },
      doneVisibility: "show",
    });

    expect(ids(projection.visibleTasks)).toEqual(["T-1", "T-2"]);
    expect(projection.hiddenCompletedTaskIds).toEqual([]);
    expectExactPartition(projection);
  });

  it("hides Done under doneVisibility=hide with a non-Done status filter", () => {
    const projection = project({ tasks, doneVisibility: "hide" });
    expect(ids(projection.visibleTasks)).toEqual(["T-1"]);
  });
});

describe("V03 normalizer", () => {
  it("resolves the four bound cases", () => {
    expect(normalizeDoneVisibility(undefined, false)).toBe("default");
    expect(normalizeDoneVisibility(undefined, true)).toBe("show");
    expect(normalizeDoneVisibility("default", true)).toBe("default");
    expect(normalizeDoneVisibility("nonsense")).toBe("default");
  });

  it("retains every valid supplied value and rejects nullish or wrong types", () => {
    expect(normalizeDoneVisibility("hide")).toBe("hide");
    expect(normalizeDoneVisibility("show")).toBe("show");
    expect(normalizeDoneVisibility(null, true)).toBe("default");
    expect(normalizeDoneVisibility(3)).toBe("default");
    expect(normalizeDoneVisibility({})).toBe("default");
  });

  it("survives the serialization contract: a normalized show is durable", () => {
    // A legacy ?status=all link normalizes to show, and re-normalizing that
    // stored value keeps show even though the legacy flag is gone.
    const fromLegacyUrl = normalizeDoneVisibility(undefined, true);
    expect(normalizeDoneVisibility(fromLegacyUrl, false)).toBe("show");
  });
});

describe("V04 legacy saved All versus a newly saved default", () => {
  const tasks = deepFreezeTasks([task("T-1"), task("T-2", { status: "done" })]);

  it("distinguishes a legacy omitted coordinate from a new explicit default", () => {
    const legacySavedAll = normalizeDoneVisibility(undefined, true);
    const newlySavedDefault = normalizeDoneVisibility("default", false);
    expect(legacySavedAll).toBe("show");
    expect(newlySavedDefault).toBe("default");

    expect(ids(project({ tasks, doneVisibility: legacySavedAll }).visibleTasks))
      .toEqual(["T-1", "T-2"]);
    expect(ids(project({ tasks, doneVisibility: newlySavedDefault }).visibleTasks))
      .toEqual(["T-1"]);
  });

  it("does not invalidate an otherwise valid legacy row carrying a bad value", () => {
    // Normalization degrades to default; it never signals row deletion.
    expect(normalizeDoneVisibility("all")).toBe("default");
  });
});

describe("V05 canonical readiness still resolves completed prerequisites", () => {
  const tasks = deepFreezeTasks([
    task("A", { dependencies: ["B"] }),
    task("B", { status: "done" }),
  ]);

  it("keeps A ready because the existing filter saw the Done prerequisite", () => {
    const filters = { readiness: "ready" } as const;
    // Control: the untouched existing filter is what decides readiness.
    expect(ids(filterWorkspaceTasks(tasks, filters))).toEqual(["A"]);

    const projection = project({ tasks, filters });
    expect(ids(projection.visibleTasks)).toEqual(["A"]);
    expect(projection.counts.filterMatchedTotal).toBe(1);
    expectExactPartition(projection);
  });

  it("classifies the completed prerequisite as other-filter, never missing", () => {
    const projection = project({ tasks, filters: { readiness: "ready" } });
    expect(projection.prerequisitesByTaskId["A"]).toEqual([
      { id: "B", reason: "other-filter", revealable: false },
    ]);
  });

  it("classifies it as completed when no other filter excludes it", () => {
    const projection = project({ tasks });
    expect(projection.prerequisitesByTaskId["A"]).toEqual([
      { id: "B", reason: "completed", revealable: true },
    ]);
  });
});

describe("V06 open child of a hidden Done parent, and no synthesized bypass", () => {
  const tasks = deepFreezeTasks([
    task("A", { parent_id: "B", dependencies: ["B"] }),
    task("B", { status: "done", dependencies: ["C"] }),
    task("C", { status: "done" }),
  ]);

  it("keeps the open child visible with its original parent and dependency IDs", () => {
    const projection = project({ tasks });

    expect(ids(projection.visibleTasks)).toEqual(["A"]);
    const child = projection.visibleTasks[0];
    expect(child.parent_id).toBe("B");
    expect(child.dependencies).toEqual(["B"]);
    expectExactPartition(projection);
  });

  it("never introduces an A to C dependency through the hidden B", () => {
    const projection = project({ tasks });
    const classified = projection.prerequisitesByTaskId["A"];
    expect(classified.map((entry) => entry.id)).toEqual(["B"]);
    expect(classified.map((entry) => entry.id)).not.toContain("C");
  });

  it("preserves an A to C dependency that already exists canonically", () => {
    const direct = deepFreezeTasks([
      task("A", { dependencies: ["B", "C"] }),
      task("B", { status: "done" }),
      task("C", { status: "done" }),
    ]);
    const projection = project({ tasks: direct });
    expect(projection.prerequisitesByTaskId["A"].map((entry) => entry.id)).toEqual([
      "B",
      "C",
    ]);
  });
});

describe("V07 three independent pins including the last visible Task", () => {
  const tasks = deepFreezeTasks([
    task("A", { status: "done" }),
    task("B", { status: "done" }),
    task("C", { status: "done" }),
  ]);

  it("retains all three distinct IDs with ordered reasons", () => {
    const projection = project({
      tasks,
      selectedTaskId: "A",
      contextTargetTaskId: "B",
      focusPinnedTaskId: "C",
    });

    expect(ids(projection.visibleTasks)).toEqual(["A", "B", "C"]);
    expect(projection.retainedTaskIds).toEqual(["A", "B", "C"]);
    expect(projection.counts.retained).toBe(3);
    expect(projection.pinReasonsByTaskId).toEqual({
      A: ["selected"],
      B: ["context"],
      C: ["focus"],
    });
    expect(projection.emptyKind).toBe("none");
    expectExactPartition(projection);
  });

  it("stays none while any pin holds and only empties once all release", () => {
    const held = project({ tasks, selectedTaskId: "A" });
    expect(held.emptyKind).toBe("none");
    expect(held.counts.visible).toBe(1);

    const released = project({ tasks });
    expect(released.emptyKind).toBe("all-complete");
    expect(released.counts.visible).toBe(0);
    expectExactPartition(released);
  });

  it("releasing one reason leaves the other pins independent", () => {
    const projection = project({ tasks, contextTargetTaskId: "B", focusPinnedTaskId: "C" });
    expect(projection.retainedTaskIds).toEqual(["B", "C"]);
    expect(projection.selection.selectedTaskId).toBeNull();
  });
});

describe("V08 other-filter beats completed and missing stays distinct", () => {
  const tasks = deepFreezeTasks([
    task("A", { dependencies: ["B", "C", "GONE"] }),
    task("B", { status: "done", priority: "P0" }),
    task("C", { status: "done" }),
  ]);

  it("prefers other-filter when both exclusions apply", () => {
    const projection = project({ tasks, filters: { priority: "P2" } });

    expect(projection.prerequisitesByTaskId["A"]).toEqual([
      { id: "B", reason: "other-filter", revealable: false },
      { id: "C", reason: "completed", revealable: true },
      { id: "GONE", reason: "missing", revealable: false },
    ]);
    expectExactPartition(projection);
  });

  it("refuses to reveal a target that another filter also excludes", () => {
    const filters = { priority: "P2" } as const;
    const scopeKey = completedVisibilityScopeKey("graph", filters, "default");
    const projection = project({
      tasks,
      filters,
      prerequisiteAnchorTaskId: "A",
      reveal: { anchorTaskId: "A", scopeKey, taskIds: ["B", "GONE", "C"] },
    });

    expect(projection.reveal?.taskIds).toEqual(["C"]);
    expect(ids(projection.visibleTasks)).toEqual(["A", "C"]);
  });
});

describe("V09 reveal cap with a full remaining count", () => {
  const dependencyIds = Array.from({ length: 12 }, (_, index) => `D-${index + 1}`);
  const tasks = deepFreezeTasks([
    task("A", { dependencies: dependencyIds }),
    ...dependencyIds.map((id) => task(id, { status: "done" })),
  ]);
  const scopeKey = completedVisibilityScopeKey("graph", {}, "default");

  it("caps at ten unique direct targets and reports the true remainder", () => {
    const projection = project({
      tasks,
      prerequisiteAnchorTaskId: "A",
      reveal: {
        anchorTaskId: "A",
        scopeKey,
        // duplicates, an unrelated ID and a missing ID must all be ignored
        taskIds: [...dependencyIds, "D-1", "A", "NOPE"],
      },
    });

    expect(projection.reveal?.taskIds).toHaveLength(REVEAL_DISPLAY_LIMIT);
    expect(projection.reveal?.taskIds).toEqual(dependencyIds.slice(0, 10));
    expect(projection.counts.revealed).toBe(10);
    expect(projection.revealRemainingCount).toBe(2);
    expectExactPartition(projection);
  });

  it("reports the remainder as data, never truncating the classification", () => {
    const projection = project({
      tasks,
      prerequisiteAnchorTaskId: "A",
      reveal: { anchorTaskId: "A", scopeKey, taskIds: dependencyIds.slice(0, 10) },
    });
    // All 12 prerequisites remain classified even though only 10 are revealed.
    expect(projection.prerequisitesByTaskId["A"]).toHaveLength(12);
    expect(
      projection.prerequisitesByTaskId["A"].filter((entry) => entry.reason === "completed"),
    ).toHaveLength(2);
  });

  it("does not traverse into transitive dependencies", () => {
    const chain = deepFreezeTasks([
      task("A", { dependencies: ["B"] }),
      task("B", { status: "done", dependencies: ["C"] }),
      task("C", { status: "done" }),
    ]);
    const key = completedVisibilityScopeKey("graph", {}, "default");
    const projection = project({
      tasks: chain,
      prerequisiteAnchorTaskId: "A",
      reveal: { anchorTaskId: "A", scopeKey: key, taskIds: ["B", "C"] },
    });

    expect(projection.reveal?.taskIds).toEqual(["B"]);
    expect(projection.revealRemainingCount).toBe(0);
  });
});

describe("V10 stale reveal scope, wrong anchor and reopened targets", () => {
  const tasks = deepFreezeTasks([
    task("A", { dependencies: ["B"] }),
    task("B", { status: "done" }),
    task("Z"),
  ]);
  const scopeKey = completedVisibilityScopeKey("graph", {}, "default");

  it("clears a reveal whose scope key no longer matches", () => {
    const projection = project({
      tasks,
      view: "board",
      prerequisiteAnchorTaskId: "A",
      reveal: { anchorTaskId: "A", scopeKey, taskIds: ["B"] },
    });

    expect(projection.reveal).toBeNull();
    expect(projection.revealRemainingCount).toBe(0);
    expect(ids(projection.visibleTasks)).toEqual(["A", "Z"]);
  });

  it("clears a reveal whose anchor is not the current anchor", () => {
    const projection = project({
      tasks,
      prerequisiteAnchorTaskId: "Z",
      reveal: { anchorTaskId: "A", scopeKey, taskIds: ["B"] },
    });
    expect(projection.reveal).toBeNull();
  });

  it("prunes a reopened target so recompletion cannot resurrect old intent", () => {
    const reopened = deepFreezeTasks([
      task("A", { dependencies: ["B"] }),
      task("B", { status: "open" }),
      task("Z"),
    ]);
    const projection = project({
      tasks: reopened,
      prerequisiteAnchorTaskId: "A",
      reveal: { anchorTaskId: "A", scopeKey, taskIds: ["B"] },
    });

    expect(projection.reveal?.taskIds).toEqual([]);
    expect(projection.counts.revealed).toBe(0);
  });

  it("drops a target that no longer exists canonically", () => {
    const projection = project({
      tasks,
      prerequisiteAnchorTaskId: "A",
      reveal: { anchorTaskId: "A", scopeKey, taskIds: ["REMOVED"] },
    });
    expect(projection.reveal?.taskIds).toEqual([]);
    expect(projection.revealRemainingCount).toBe(1);
  });

  it("clears the reveal when the anchor itself is not visible", () => {
    const hiddenAnchor = deepFreezeTasks([
      task("A", { status: "done", dependencies: ["B"] }),
      task("B", { status: "done" }),
    ]);
    const projection = project({
      tasks: hiddenAnchor,
      prerequisiteAnchorTaskId: "A",
      reveal: {
        anchorTaskId: "A",
        scopeKey: completedVisibilityScopeKey("graph", {}, "default"),
        taskIds: ["B"],
      },
    });
    expect(projection.reveal).toBeNull();
  });
});

describe("V11 empty-kind precedence", () => {
  it("reports no-tasks for an empty canonical workspace", () => {
    const projection = project({ tasks: deepFreezeTasks([]) });
    expect(projection.emptyKind).toBe("no-tasks");
    expect(projection.counts).toMatchObject({ canonicalTotal: 0, visible: 0 });
    expectExactPartition(projection);
  });

  it("reports all-complete when every matching Task is Done", () => {
    const tasks = deepFreezeTasks([
      task("T-1", { status: "done" }),
      task("T-2", { status: "done" }),
    ]);
    const projection = project({ tasks });

    expect(projection.emptyKind).toBe("all-complete");
    expect(projection.counts.hiddenCompleted).toBe(projection.counts.filterMatchedTotal);
    expectExactPartition(projection);
  });

  it("reports other-filter-empty when a search excludes everything", () => {
    const tasks = deepFreezeTasks([task("T-1"), task("T-2", { status: "done" })]);
    const projection = project({ tasks, filters: { search: "no-such-text" } });

    expect(projection.emptyKind).toBe("other-filter-empty");
    expect(projection.counts.filterMatchedTotal).toBe(0);
    expectExactPartition(projection);
  });

  it("never claims all-complete while another filter is hiding open work", () => {
    const tasks = deepFreezeTasks([
      task("T-1", { status: "done", priority: "P2" }),
      task("T-2", { status: "open", priority: "P0" }),
    ]);
    const projection = project({ tasks, filters: { priority: "P2" } });

    expect(projection.counts.filterMatchedTotal).toBe(1);
    expect(projection.emptyKind).toBe("all-complete");
    // The open Task is still accounted for as hidden by another filter.
    expect(projection.hiddenOtherTaskIds).toEqual(["T-2"]);
    expectExactPartition(projection);
  });
});

describe("V12 pins outside filters, overlapping pins and missing pin IDs", () => {
  const tasks = deepFreezeTasks([task("A", { priority: "P0" }), task("B")]);

  it("retains a pinned Task that the current filters reject", () => {
    const projection = project({
      tasks,
      filters: { priority: "P2" },
      selectedTaskId: "A",
    });

    expect(ids(projection.visibleTasks)).toEqual(["A", "B"]);
    expect(projection.retainedTaskIds).toEqual(["A"]);
    expect(projection.counts.filterMatchedTotal).toBe(1);
    expect(projection.counts.retained).toBe(1);
    expectExactPartition(projection);
  });

  it("deduplicates overlapping pins into one Task with several reasons", () => {
    const projection = project({
      tasks,
      selectedTaskId: "A",
      contextTargetTaskId: "A",
      focusPinnedTaskId: "A",
    });

    expect(projection.pinReasonsByTaskId["A"]).toEqual(["selected", "context", "focus"]);
    expect(ids(projection.visibleTasks).filter((id) => id === "A")).toHaveLength(1);
    expectExactPartition(projection);
  });

  it("reports a missing pin ID without fabricating a Task or clearing state", () => {
    const projection = project({ tasks, contextTargetTaskId: "GHOST" });

    expect(projection.missingPinTaskIds).toEqual(["GHOST"]);
    expect(ids(projection.visibleTasks)).toEqual(["A", "B"]);
    expect(projection.selection.contextTargetTaskId).toBe("GHOST");
    expectExactPartition(projection);
  });
});

describe("V13 purity: frozen inputs, identity and no mutation", () => {
  const tasks = deepFreezeTasks([
    task("A", { dependencies: ["B"], parent_id: "B", revision: 7 }),
    task("B", { status: "done", dependencies: ["C"] }),
    task("C", { status: "done" }),
  ]);

  it("returns the exact same canonical array reference", () => {
    const projection = project({ tasks });
    expect(projection.referenceTasks).toBe(tasks);
  });

  it("returns original Task object references in canonical source order", () => {
    const projection = project({ tasks, doneVisibility: "show" });
    expect(projection.visibleTasks[0]).toBe(tasks[0]);
    expect(projection.visibleTasks[1]).toBe(tasks[1]);
    expect(projection.visibleTasks[2]).toBe(tasks[2]);
    expect(ids(projection.visibleTasks)).toEqual(["A", "B", "C"]);
  });

  it("does not sort in place or mutate any canonical field", () => {
    const before = tasks.map((item) => JSON.stringify(item));
    const scopeKey = completedVisibilityScopeKey("graph", {}, "default");

    project({
      tasks,
      selectedTaskId: "B",
      prerequisiteAnchorTaskId: "A",
      reveal: { anchorTaskId: "A", scopeKey, taskIds: ["B"] },
    });

    expect(ids(tasks)).toEqual(["A", "B", "C"]);
    expect(tasks.map((item) => JSON.stringify(item))).toEqual(before);
    expect(tasks[0].status).toBe("open");
    expect(tasks[0].parent_id).toBe("B");
    expect(tasks[0].dependencies).toEqual(["B"]);
    expect(tasks[0].revision).toBe(7);
  });

  it("produces a new visible array rather than aliasing the canonical one", () => {
    const projection = project({ tasks, doneVisibility: "show" });
    expect(projection.visibleTasks).not.toBe(tasks);
  });
});

describe("V14 explicit Done does not bypass unrelated filters", () => {
  const tasks = deepFreezeTasks([
    task("A", { status: "done" }),
    task("B", { status: "done", priority: "P0" }),
    task("C", { status: "open" }),
  ]);

  it("intersects status=done with a contradictory readiness filter", () => {
    const filters = { status: "done", readiness: "ready" } as const;
    // Control: readiness rejects every completed Task in the existing filter.
    expect(filterWorkspaceTasks(tasks, filters)).toEqual([]);

    const projection = project({ tasks, filters, doneVisibility: "show" });
    expect(projection.counts.filterMatchedTotal).toBe(0);
    expect(ids(projection.visibleTasks)).toEqual([]);
    expect(projection.emptyKind).toBe("other-filter-empty");
    expectExactPartition(projection);
  });

  it("still applies priority while status=done overrides hiding", () => {
    const projection = project({
      tasks,
      filters: { status: "done", priority: "P0" },
    });

    expect(ids(projection.visibleTasks)).toEqual(["B"]);
    expect(projection.hiddenOtherTaskIds).toEqual(["A", "C"]);
    expectExactPartition(projection);
  });
});

describe("date purity", () => {
  const tasks = deepFreezeTasks([
    task("A", { due: "2026-09-02" }),
    task("B", { status: "done", due: "2026-09-02" }),
  ]);

  it("never reads a clock: a timing filter without a caller date matches nothing", () => {
    const filters = { timing: "today" } as const;
    // Control: the existing filter itself refuses to invent a date.
    expect(filterWorkspaceTasks(tasks, filters)).toEqual([]);

    const projection = project({ tasks, filters });
    expect(projection.counts.filterMatchedTotal).toBe(0);
    expect(projection.emptyKind).toBe("other-filter-empty");
  });

  it("uses the caller-supplied frozen date and nothing else", () => {
    const filters = { timing: "today", today: "2026-09-02" } as const;
    // Control: the existing timing filter rejects any non-open/started Task
    // before it compares due dates, so the Done Task is excluded by that filter
    // rather than by the Done rule. hiddenOther, not hiddenCompleted.
    expect(ids(filterWorkspaceTasks(tasks, filters))).toEqual(["A"]);

    const projection = project({ tasks, filters });
    expect(ids(projection.visibleTasks)).toEqual(["A"]);
    expect(projection.hiddenOtherTaskIds).toEqual(["B"]);
    expect(projection.hiddenCompletedTaskIds).toEqual([]);
    expectExactPartition(projection);
  });

  it("stays deterministic under a throwing clock", () => {
    // Only the clock is poisoned. Parsing the caller's supplied date string is
    // legitimate and must keep working, so Date construction is left intact.
    const realNow = Date.now;
    Date.now = () => {
      throw new Error("clock must not be read by the pure projection");
    };
    try {
      const projection = project({
        tasks,
        filters: { timing: "today", today: "2026-09-02" },
      });
      expect(ids(projection.visibleTasks)).toEqual(["A"]);
      expect(normalizeDoneVisibility(undefined, true)).toBe("show");
      expect(completedVisibilityScopeKey("graph", { today: "2026-09-02" }, "default"))
        .toContain("2026-09-02");
    } finally {
      Date.now = realNow;
    }
  });

  it("includes the supplied date in the scope key so a date change invalidates reveal", () => {
    const monday = completedVisibilityScopeKey("graph", { today: "2026-09-02" }, "default");
    const tuesday = completedVisibilityScopeKey("graph", { today: "2026-09-03" }, "default");
    const undated = completedVisibilityScopeKey("graph", {}, "default");

    expect(monday).not.toBe(tuesday);
    expect(undated).not.toBe(monday);
    expect(JSON.parse(undated)[10]).toBeNull();
  });
});

describe("scope key", () => {
  it("changes with the view, every filter axis and the visibility coordinate", () => {
    const base = completedVisibilityScopeKey("graph", {}, "default");
    expect(completedVisibilityScopeKey("board", {}, "default")).not.toBe(base);
    expect(completedVisibilityScopeKey("graph", {}, "show")).not.toBe(base);
    expect(completedVisibilityScopeKey("graph", { search: "x" }, "default")).not.toBe(base);
    expect(completedVisibilityScopeKey("graph", { status: "done" }, "default")).not.toBe(base);
    expect(completedVisibilityScopeKey("graph", { priority: "P0" }, "default")).not.toBe(base);
    expect(completedVisibilityScopeKey("graph", { objectiveId: "O-1" }, "default")).not.toBe(base);
    expect(completedVisibilityScopeKey("graph", { readiness: "ready" }, "default")).not.toBe(base);
    expect(completedVisibilityScopeKey("graph", { timing: "overdue" }, "default")).not.toBe(base);
  });

  it("treats omitted filters as their documented defaults", () => {
    expect(completedVisibilityScopeKey("graph", {}, "default")).toBe(
      completedVisibilityScopeKey(
        "graph",
        {
          search: "",
          status: "all",
          priority: "all",
          objectiveId: "all",
          readiness: "all",
          timing: "all",
        },
        "default",
      ),
    );
  });
});


describe("outcome scope", () => {
  const key = (outcome: unknown) =>
    completedVisibilityScopeKey("graph", { outcome } as never, "default");

  it("invalidates on kind, pair and parent changes and normalizes malformed input", () => {
    const all = key({ kind: "all" });
    const unassigned = key({ kind: "unassigned" });
    const pair = key({ kind: "pair", objectiveId: "O-1", keyResultId: "KR-1" });
    const otherParent = key({ kind: "pair", objectiveId: "O-2", keyResultId: "KR-1" });
    const otherResult = key({ kind: "pair", objectiveId: "O-1", keyResultId: "KR-2" });

    expect(new Set([all, unassigned, pair, otherParent, otherResult]).size).toBe(5);
    expect(key(undefined)).toBe(all);
    expect(key({ kind: "nope" })).toBe(all);
    expect(key({ kind: "pair", objectiveId: " ", keyResultId: "KR-1" })).toBe(all);
    expect(JSON.parse(pair).slice(7, 10)).toEqual(["pair", "O-1", "KR-1"]);
  });
});
