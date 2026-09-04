import { describe, expect, test } from "vitest";
import type { Objective, Task } from "../../../domain/types";
import { keyResultKey, projectKeyResults } from "./keyResultModel";
import {
  DERIVED_KEY_RESULT_OBJECTIVE,
  DERIVED_TASK_KEY_RESULT,
  buildOutcomeTreemap,
  deriveOutcomeEdges,
  keyResultCatalog,
  outcomeChipsForTask,
  taskEndpointKey,
} from "./keyResultViewModel";
import type { WorkspaceTask } from "./types";

function objective(id: string, keyResults: Objective["key_results"]): Objective {
  return { id, objective: `${id} objective`, revision: 1, key_results: keyResults };
}

function task(id: string, overrides: Partial<Task> = {}): Task {
  return {
    id,
    uid: `00000000-0000-4000-8000-00000000000${id.slice(-1)}`,
    title: `${id} title`,
    detail: "",
    status: "open",
    priority: "P2",
    due: null,
    tags: [],
    objective_ids: [],
    parent_id: null,
    dependencies: [],
    subtasks: [],
    notes: [],
    revision: 1,
    context_count: 0,
    ...overrides,
  };
}

const A = objective("O-A", [
  { id: "KR-1", text: "A outcome" },
  { id: "KR-2", text: "Zero linked" },
]);
const B = objective("O-B", [{ id: "KR-1", text: "B outcome" }]);

const single = task("T-1", {
  objective_ids: ["O-A"],
  key_result_refs: [{ objective_id: "O-A", key_result_id: "KR-1" }],
});
const multiple = task("T-2", {
  objective_ids: ["O-A", "O-B"],
  key_result_refs: [
    { objective_id: "O-A", key_result_id: "KR-1" },
    { objective_id: "O-B", key_result_id: "KR-1" },
  ],
});
const unresolved = task("T-3", {
  objective_ids: ["O-A"],
  key_result_refs: [{ objective_id: "O-A", key_result_id: "KR-9" }],
});
const aligned = task("T-4", { objective_ids: ["O-B"] });
const operational = task("T-5");

const TASKS = [single, multiple, unresolved, aligned, operational];

function project(visible: readonly Task[] = TASKS) {
  return projectKeyResults({
    workspaceId: "W1",
    tasks: TASKS,
    visibleTasks: visible,
    objectives: [A, B],
  });
}

describe("derived edges", () => {
  test("only visible resolved refs and every displayed KR produce derived edges", () => {
    const projection = project();

    const edges = deriveOutcomeEdges(projection, [single, unresolved] as WorkspaceTask[]);

    const taskEdges = edges.filter((item) => item.kind === DERIVED_TASK_KEY_RESULT);
    expect(taskEdges).toHaveLength(1);
    expect(taskEdges[0].source).toBe(taskEndpointKey("T-1"));
    expect(edges.every((item) => item.derived)).toBe(true);
    expect(edges.filter((item) => item.kind === DERIVED_KEY_RESULT_OBJECTIVE)).toHaveLength(3);
    expect(new Set(edges.map((item) => item.id)).size).toBe(edges.length);
  });

  test("endpoint keys stay collision-safe for delimiter-bearing identifiers", () => {
    const tricky = objective("O|A", [{ id: "KR|1", text: "Tricky" }]);
    const linked = task("T-9", {
      objective_ids: ["O|A"],
      key_result_refs: [{ objective_id: "O|A", key_result_id: "KR|1" }],
    });
    const projection = projectKeyResults({
      workspaceId: "W1",
      tasks: [linked],
      objectives: [tricky],
    });

    const edges = deriveOutcomeEdges(projection, [linked] as WorkspaceTask[]);

    expect(new Set(edges.map((item) => item.id)).size).toBe(edges.length);
    expect(edges.every((item) => item.source !== item.target)).toBe(true);
  });

  test("a Task hidden from the visible subset contributes no Task edge", () => {
    const projection = project([]);

    const edges = deriveOutcomeEdges(projection, []);

    expect(edges.filter((item) => item.kind === DERIVED_TASK_KEY_RESULT)).toHaveLength(0);
    expect(edges.filter((item) => item.kind === DERIVED_KEY_RESULT_OBJECTIVE)).toHaveLength(3);
  });
});

describe("task chips", () => {
  test("resolved and unresolved refs both survive with their original pair", () => {
    const projection = project();

    expect(outcomeChipsForTask(projection, "T-1")).toEqual({
      taskId: "T-1",
      placement: "single-outcome",
      chips: [{
        key: ["chip", "T-1", keyResultKey("W1", "O-A", "KR-1")].join("|"),
        objectiveId: "O-A",
        keyResultId: "KR-1",
        label: "O-A · KR-1",
        resolved: true,
        text: "A outcome",
        reason: null,
      }],
    });

    const dangling = outcomeChipsForTask(projection, "T-3");
    expect(dangling.placement).toBe("unresolved-outcome");
    expect(dangling.chips[0]).toMatchObject({ label: "O-A · KR-9", resolved: false, text: null });
  });

  test("a Task with no refs has no chips and stays unassigned", () => {
    const projection = project();

    expect(outcomeChipsForTask(projection, "T-5")).toEqual({
      taskId: "T-5",
      placement: "unassigned-outcome",
      chips: [],
    });
  });
});

describe("treemap hierarchy", () => {
  const size = (item: WorkspaceTask) => 1 + (item.subtasks?.length ?? 0);

  test("each visible Task is placed exactly once under a typed node", () => {
    const projection = project();

    const tree = buildOutcomeTreemap(projection, TASKS as WorkspaceTask[], size);

    const leaves = tree.flatMap((objectiveNode) => objectiveNode.children.flatMap((group) => group.children));
    expect(leaves.map((leaf) => leaf.id).sort()).toEqual(["T-1", "T-2", "T-3", "T-4", "T-5"]);
    expect(new Set(leaves.map((leaf) => leaf.id)).size).toBe(leaves.length);
    expect(leaves.every((leaf) => leaf.nodeKind === "task" && leaf.size === 1)).toBe(true);
  });

  test("multiple, unresolved and unassigned each use one labelled bucket", () => {
    const projection = project();

    const tree = buildOutcomeTreemap(projection, TASKS as WorkspaceTask[], size);
    const groups = tree.flatMap((objectiveNode) => objectiveNode.children);

    const names = groups.map((group) => group.name);
    expect(names).toContain("Multiple outcomes");
    expect(names).toContain("Unresolved outcome");
    expect(names).toContain("Unassigned outcome");
    const single = groups.find((group) => group.nodeKind === "key-result");
    expect(single).toMatchObject({ objectiveId: "O-A", keyResultId: "KR-1" });
    expect(single?.children.map((leaf) => leaf.id)).toEqual(["T-1"]);
  });

  test("an unassigned Task keeps its Objective alignment or falls back to Operations", () => {
    const projection = project();

    const tree = buildOutcomeTreemap(projection, [aligned, operational] as WorkspaceTask[], size);

    const alignedGroup = tree.find((node) => node.objectiveId === "O-B");
    const operationsGroup = tree.find((node) => node.name === "Operations");
    expect(alignedGroup?.children[0].children.map((leaf) => leaf.id)).toEqual(["T-4"]);
    expect(operationsGroup?.children[0].children.map((leaf) => leaf.id)).toEqual(["T-5"]);
  });

  test("the supplied size value is used verbatim and never duplicated per membership", () => {
    const projection = project();

    const tree = buildOutcomeTreemap(projection, [multiple] as WorkspaceTask[], () => 7);
    const leaves = tree.flatMap((node) => node.children.flatMap((group) => group.children));

    expect(leaves).toHaveLength(1);
    expect(leaves[0].size).toBe(7);
  });
});

describe("catalog", () => {
  test("zero-linked Key Results remain in the catalog even with no visible Tasks", () => {
    const projection = project([]);

    const catalog = keyResultCatalog(projection);

    expect(catalog.map((node) => node.key)).toEqual([
      keyResultKey("W1", "O-A", "KR-1"),
      keyResultKey("W1", "O-A", "KR-2"),
      keyResultKey("W1", "O-B", "KR-1"),
    ]);
    const zeroLinked = catalog.find((node) => node.keyResultId === "KR-2");
    expect(zeroLinked?.counts.total).toBe(0);
    expect(zeroLinked?.recordedProgress).toBeNull();
  });

  test("inputs are never mutated by the view model", () => {
    const projection = project();
    const before = JSON.stringify(projection);

    deriveOutcomeEdges(projection, TASKS as WorkspaceTask[]);
    buildOutcomeTreemap(projection, TASKS as WorkspaceTask[], () => 1);
    outcomeChipsForTask(projection, "T-2");

    expect(JSON.stringify(projection)).toBe(before);
  });
});


describe("NG2 chip identity is collision-safe and workspace-scoped", () => {
  const adversarial = (workspaceId: string) => projectKeyResults({
    workspaceId,
    tasks: [task("T-1", {
      objective_ids: ["O · K", "O"],
      key_result_refs: [
        { objective_id: "O · K", key_result_id: "Z" },
        { objective_id: "O", key_result_id: "K · Z" },
      ],
    })],
    objectives: [
      objective("O · K", [{ id: "Z", text: "Left" }]),
      objective("O", [{ id: "K · Z", text: "Right" }]),
    ],
  });

  test("two distinct read coordinates never share one chip key", () => {
    const chips = outcomeChipsForTask(adversarial("W-A"), "T-1").chips;

    expect(chips).toHaveLength(2);
    expect(new Set(chips.map((chip) => chip.key)).size).toBe(2);
    expect(chips.map((chip) => chip.label)).toEqual(["O · K · Z", "O · K · Z"]);
    expect(chips.map((chip) => [chip.objectiveId, chip.keyResultId])).toEqual([
      ["O · K", "Z"],
      ["O", "K · Z"],
    ]);
  });

  test("changing only the workspace changes chip identity", () => {
    const here = outcomeChipsForTask(adversarial("W-A"), "T-1").chips;
    const there = outcomeChipsForTask(adversarial("W-B"), "T-1").chips;

    expect(here[0].key).not.toBe(there[0].key);
  });
});

describe("NG3 grouping keys are typed rather than a reserved identifier", () => {
  test("a real Objective named __none__ stays separate from Operations", () => {
    const sentinel = objective("__none__", [{ id: "KR-1", text: "Sentinel outcome" }]);
    const linked = task("T-1", {
      objective_ids: ["__none__"],
      key_result_refs: [{ objective_id: "__none__", key_result_id: "KR-1" }],
    });
    const operational = task("T-2");
    const projection = projectKeyResults({
      workspaceId: "W1",
      tasks: [linked, operational],
      objectives: [sentinel],
    });

    const tree = buildOutcomeTreemap(projection, [linked, operational] as WorkspaceTask[], () => 1);

    expect(tree).toHaveLength(2);
    expect(new Set(tree.map((node) => node.key)).size).toBe(2);
    const operations = tree.find((node) => node.objectiveId === null);
    const real = tree.find((node) => node.objectiveId === "__none__");
    expect(operations?.children[0].children.map((leaf) => leaf.id)).toEqual(["T-2"]);
    expect(real?.children[0].children.map((leaf) => leaf.id)).toEqual(["T-1"]);
  });
});

describe("NG4 the unresolved reason survives into chip data", () => {
  test("an unresolved chip carries the exact model reason and its stored pair", () => {
    const linked = task("T-1", {
      objective_ids: ["O-A"],
      key_result_refs: [{ objective_id: "O-A", key_result_id: "KR-404" }],
    });
    const projection = projectKeyResults({
      workspaceId: "W1",
      tasks: [linked],
      objectives: [objective("O-A", [{ id: "KR-1", text: "A outcome" }])],
    });

    const [chip] = outcomeChipsForTask(projection, "T-1").chips;

    expect(chip.resolved).toBe(false);
    expect(chip.reason).toBe("missing-key-result");
    expect([chip.objectiveId, chip.keyResultId]).toEqual(["O-A", "KR-404"]);
  });

  test("healthy control: a resolved chip carries no reason", () => {
    const linked = task("T-1", {
      objective_ids: ["O-A"],
      key_result_refs: [{ objective_id: "O-A", key_result_id: "KR-1" }],
    });
    const projection = projectKeyResults({
      workspaceId: "W1",
      tasks: [linked],
      objectives: [objective("O-A", [{ id: "KR-1", text: "A outcome" }])],
    });

    const [chip] = outcomeChipsForTask(projection, "T-1").chips;

    expect(chip.resolved).toBe(true);
    expect(chip.reason).toBeNull();
  });
});
