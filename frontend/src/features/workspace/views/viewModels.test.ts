import { describe, expect, it } from "vitest";

import type {
  WorkspaceNote,
  WorkspaceObjective,
  WorkspaceTask,
} from "./types";
import {
  buildTreemapGroups,
  buildWorkspaceEdges,
  filterWorkspaceTasks,
  tasksByStatus,
} from "./viewModels";

const objectives: WorkspaceObjective[] = [
  { id: "O-1", objective: "Reliable releases" },
  { id: "O-2", objective: "Calm operations" },
];

const tasks: WorkspaceTask[] = [
  {
    id: "T-0001",
    title: "Release gate",
    detail: "Define checks",
    status: "started",
    priority: "P0",
    objective_ids: ["O-1"],
    dependencies: [],
    tags: ["quality"],
    context_count: 2,
  },
  {
    id: "T-0002",
    title: "Recovery guide",
    status: "open",
    priority: "P2",
    objective_ids: [],
    dependencies: ["T-0001"],
  },
];

describe("workspace view models", () => {
  it("applies all shared filters together", () => {
    expect(
      filterWorkspaceTasks(tasks, {
        search: "quality",
        status: "started",
        priority: "P0",
        objectiveId: "O-1",
      }).map((task) => task.id),
    ).toEqual(["T-0001"]);
    expect(filterWorkspaceTasks(tasks, { status: "done" })).toEqual([]);
  });

  it("filters active work by dependency readiness against the complete task set", () => {
    const completed: WorkspaceTask = {
      ...tasks[0],
      id: "T-0003",
      status: "done",
      dependencies: [],
    };

    expect(filterWorkspaceTasks([...tasks, completed], { readiness: "ready" }).map((task) => task.id))
      .toEqual(["T-0001"]);
    expect(filterWorkspaceTasks([...tasks, completed], { readiness: "blocked" }).map((task) => task.id))
      .toEqual(["T-0002"]);
  });

  it("filters active work by shared local-calendar due timing", () => {
    const dated: WorkspaceTask[] = [
      { ...tasks[0], id: "T-overdue", due: "2026-08-29", status: "open" },
      { ...tasks[0], id: "T-today", due: "2026-08-30", status: "started" },
      { ...tasks[0], id: "T-soon", due: "2026-09-03", status: "open" },
      { ...tasks[0], id: "T-none", due: null, status: "open" },
      { ...tasks[0], id: "T-done", due: "2026-08-29", status: "done" },
    ];
    const ids = (timing: string) => filterWorkspaceTasks(
      dated,
      { timing, today: "2026-08-30" } as never,
    ).map((task) => task.id);

    expect(ids("overdue")).toEqual(["T-overdue"]);
    expect(ids("today")).toEqual(["T-today"]);
    expect(ids("soon")).toEqual(["T-soon"]);
    expect(ids("unscheduled")).toEqual(["T-none"]);
  });

  it("keeps a ten-thousand Task readiness filter inside the bounded scale gate", () => {
    const large = Array.from({ length: 10_000 }, (_, index): WorkspaceTask => {
      const id = `T-${String(index + 1).padStart(5, "0")}`;
      return {
        id,
        title: id,
        status: "open",
        priority: "P2",
        objective_ids: [],
        dependencies: index ? [`T-${String(index).padStart(5, "0")}`] : [],
      };
    });
    const started = performance.now();
    const blocked = filterWorkspaceTasks(large, { readiness: "blocked" });
    const elapsed = performance.now() - started;

    expect(blocked).toHaveLength(9_999);
    expect(elapsed).toBeLessThan(500);
  }, 10_000);

  it("reconstructs and canonicalizes fixture relationships without duplicates", () => {
    const notes: WorkspaceNote[] = [
      { id: "N-1", text: "Gate rationale", links: ["T-0001"] },
    ];
    const edges = buildWorkspaceEdges(tasks, notes, [
      { source: "T-0001", target: "O-1", kind: "objective" },
      { source: "N-1", target: "T-0001", kind: "note" },
    ]);

    expect(edges).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          source: "T-0001",
          target: "O-1",
          kind: "alignment",
        }),
        expect.objectContaining({
          source: "T-0002",
          target: "T-0001",
          kind: "dependency",
        }),
        expect.objectContaining({
          source: "N-1",
          target: "T-0001",
          kind: "reference",
        }),
      ]),
    );
    expect(new Set(edges.map((edge) => edge.id)).size).toBe(edges.length);
  });

  it("shows multi-aligned and operational work exactly once in the treemap", () => {
    const multiAligned: WorkspaceTask = {
      ...tasks[0],
      id: "T-0003",
      objective_ids: ["O-1", "O-2"],
    };
    const groups = buildTreemapGroups([...tasks, multiAligned], objectives);
    const leaves = groups.flatMap((group) => group.children);

    expect(groups.find((group) => group.objectiveId === "O-1")?.children).toHaveLength(1);
    expect(groups.find((group) => group.objectiveId === "multiple")?.children).toEqual([
      expect.objectContaining({ taskId: "T-0003", objectiveIds: ["O-1", "O-2"] }),
    ]);
    expect(groups.find((group) => group.objectiveId === "none")?.children[0].taskId).toBe(
      "T-0002",
    );
    expect(leaves.map((leaf) => leaf.taskId).sort()).toEqual(["T-0001", "T-0002", "T-0003"]);
  });

  it("places unknown statuses safely in open and sorts by priority", () => {
    const grouped = tasksByStatus([
      ...tasks,
      { id: "T-0000", title: "Unknown", status: "queued", priority: "P1" },
    ]);
    expect(grouped.open.map((task) => task.id)).toEqual(["T-0000", "T-0002"]);
    expect(grouped.started.map((task) => task.id)).toEqual(["T-0001"]);
  });
});
