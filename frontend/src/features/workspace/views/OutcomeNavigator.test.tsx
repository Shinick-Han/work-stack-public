import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { projectOutcomeHierarchy } from "../../../domain/outcomeHierarchy";
import type { OutcomeHierarchy } from "../../../domain/outcomeHierarchy";
import type { Objective, Task } from "../../../domain/types";
import type { OutcomeFilter } from "./keyResultModel";
import { OutcomeNavigator } from "./OutcomeNavigator";

const WORKSPACE_ID = "W-oracle";

function objective(
  id: string,
  keyResults: { id: string; text: string; progress?: number }[],
): Objective {
  return {
    id,
    objective: `${id} objective`,
    title: `${id} objective`,
    revision: 1,
    key_results: keyResults.map((item) => ({
      id: item.id,
      text: item.text,
      ...(item.progress !== undefined ? { progress: item.progress } : {}),
    })),
  };
}

function task(id: string, patch: Partial<Task> = {}): Task {
  return {
    id,
    uid: `00000000-0000-4000-8000-${id.replace(/[^0-9]/g, "").padStart(12, "0")}`,
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
    ...patch,
  };
}

function section84Fixture() {
  const objectives: Objective[] = [
    objective("O-1", [
      { id: "KR-1", text: "O-1 KR-1", progress: 40 },
      { id: "KR-2", text: "O-1 KR-2" },
    ]),
    objective("O-2", [{ id: "KR-1", text: "O-2 KR-1" }]),
  ];
  const tasks: Task[] = [
    task("T-1", {
      objective_ids: ["O-1"],
      key_result_refs: [{ objective_id: "O-1", key_result_id: "KR-1" }],
    }),
    task("T-2", { objective_ids: ["O-1"] }),
    task("T-3", {
      objective_ids: ["O-1"],
      key_result_refs: [
        { objective_id: "O-1", key_result_id: "KR-1" },
        { objective_id: "O-1", key_result_id: "KR-2" },
      ],
    }),
    task("T-4"),
    task("T-5", {
      objective_ids: ["O-1"],
      key_result_refs: [{ objective_id: "O-1", key_result_id: "KR-MISSING" }],
    }),
  ];
  return { objectives, tasks };
}

function hierarchyOf(): OutcomeHierarchy {
  const { objectives, tasks } = section84Fixture();
  return projectOutcomeHierarchy({
    workspaceId: WORKSPACE_ID,
    tasks,
    visibleTasks: tasks,
    objectives,
  });
}

function emptyHierarchy(): OutcomeHierarchy {
  return {
    workspaceId: WORKSPACE_ID,
    objectiveNodes: [],
    unaligned: { taskIds: [] },
    unresolved: { taskIds: [] },
    unassignedTaskIds: [],
    projection: {
      workspaceId: WORKSPACE_ID,
      keyResults: [],
      byKey: {},
      tasks: [],
      unassignedTaskIds: [],
    },
  };
}

function renderNavigator(
  hierarchy: OutcomeHierarchy,
  filter: OutcomeFilter = { kind: "all" },
  onFilterChange = vi.fn(),
) {
  return {
    onFilterChange,
    ...render(
      <OutcomeNavigator
        hierarchy={hierarchy}
        filter={filter}
        onFilterChange={onFilterChange}
      />,
    ),
  };
}

async function openPanel() {
  const user = userEvent.setup();
  const trigger = screen.getByRole("button", { name: "Outcome navigator" });
  await user.click(trigger);
  return { user, trigger };
}

function mockNarrow(matches: boolean) {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: matches && query.includes("720"),
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

function expectStableDisclosure(trigger: HTMLElement, expanded: boolean) {
  expect(trigger).toHaveAttribute("aria-expanded", String(expanded));
  const panelId = trigger.getAttribute("aria-controls");
  expect(panelId).toBeTruthy();
  const panel = document.getElementById(panelId!);
  expect(panel).toBeTruthy();
  if (expanded) {
    expect(panel).not.toHaveAttribute("hidden");
  } else {
    expect(panel).toHaveAttribute("hidden");
  }
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("OutcomeNavigator", () => {
  it("keeps O-1/KR-1 and O-2/KR-1 as distinct buttons and pressed states", async () => {
    const { onFilterChange, rerender } = renderNavigator(hierarchyOf());
    const { user } = await openPanel();
    const o1 = screen.getByRole("button", { name: "Select outcome O-1 KR-1" });
    const o2 = screen.getByRole("button", { name: "Select outcome O-2 KR-1" });
    expect(o1).not.toBe(o2);
    expect(o1).toHaveAttribute("aria-pressed", "false");
    expect(o2).toHaveAttribute("aria-pressed", "false");

    await user.click(o1);
    expect(onFilterChange).toHaveBeenCalledExactlyOnceWith({
      kind: "pair",
      objectiveId: "O-1",
      keyResultId: "KR-1",
    });

    rerender(
      <OutcomeNavigator
        hierarchy={hierarchyOf()}
        filter={{ kind: "pair", objectiveId: "O-1", keyResultId: "KR-1" }}
        onFilterChange={onFilterChange}
      />,
    );
    expect(screen.getByRole("button", { name: "Select outcome O-1 KR-1" }))
      .toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Select outcome O-2 KR-1" }))
      .toHaveAttribute("aria-pressed", "false");
  });

  it("shows the zero-linked O-2/KR-1 row as 0 visible of 0 linked and keeps it usable", async () => {
    const { onFilterChange } = renderNavigator(hierarchyOf());
    const { user } = await openPanel();
    const row = screen.getByRole("button", { name: "Select outcome O-2 KR-1" });
    expect(within(row).getByText("0 visible of 0 linked")).toBeInTheDocument();
    expect(within(row).getByText("O-2 KR-1")).toBeInTheDocument();
    await user.click(row);
    expect(onFilterChange).toHaveBeenCalledExactlyOnceWith({
      kind: "pair",
      objectiveId: "O-2",
      keyResultId: "KR-1",
    });
  });

  it("renders supplied visible/linked counts without recounting task ids", async () => {
    const tree = hierarchyOf();
    const o1kr1 = tree.objectiveNodes[0]?.keyResults.find((node) => node.keyResultId === "KR-1");
    expect(o1kr1).toBeTruthy();
    const tampered: OutcomeHierarchy = {
      ...tree,
      objectiveNodes: tree.objectiveNodes.map((objective) => (
        objective.objectiveId !== "O-1"
          ? objective
          : {
            ...objective,
            keyResults: objective.keyResults.map((node) => (
              node.keyResultId !== "KR-1"
                ? node
                : {
                  ...node,
                  linkedTaskIds: [],
                  visibleTaskIds: [],
                  taskRefs: [],
                  counts: { total: 9, open: 0, started: 0, done: 0, dropped: 0 },
                  visibleCounts: { total: 7, open: 0, started: 0, done: 0, dropped: 0 },
                }
            )),
          }
      )),
    };
    renderNavigator(tampered);
    await openPanel();
    expect(screen.getByText("7 visible of 9 linked")).toBeInTheDocument();
    expect(screen.queryByText("0 visible of 0 linked")).toBeInTheDocument();
  });

  it("emits exact all, unassigned, and pair filters and does not invent other writes", async () => {
    const { onFilterChange } = renderNavigator(hierarchyOf(), { kind: "all" });
    const { user } = await openPanel();
    await user.click(screen.getByRole("button", { name: "Unassigned outcome" }));
    await user.click(screen.getByRole("button", { name: "All outcomes" }));
    await user.click(screen.getByRole("button", { name: "Select outcome O-1 KR-2" }));
    expect(onFilterChange.mock.calls).toEqual([
      [{ kind: "unassigned" }],
      [{ kind: "all" }],
      [{ kind: "pair", objectiveId: "O-1", keyResultId: "KR-2" }],
    ]);
  });

  it("uses native details expansion for Objective groups", async () => {
    renderNavigator(hierarchyOf());
    const { user } = await openPanel();
    const summary = screen.getByText("O-1");
    const details = summary.closest("details");
    expect(details).toHaveAttribute("open");
    await user.click(summary);
    expect(details).not.toHaveAttribute("open");
    await user.click(summary);
    expect(details).toHaveAttribute("open");
  });

  it("does not parse delimiter-bearing identifiers from display text", async () => {
    const { objectives, tasks } = section84Fixture();
    objectives.push(objective("O-1|extra", [{ id: "KR-1|x", text: "weird" }]));
    const hierarchy = projectOutcomeHierarchy({
      workspaceId: WORKSPACE_ID,
      tasks,
      objectives,
    });
    const { onFilterChange } = renderNavigator(hierarchy);
    const { user } = await openPanel();
    await user.click(screen.getByRole("button", { name: "Select outcome O-1|extra KR-1|x" }));
    expect(onFilterChange).toHaveBeenCalledExactlyOnceWith({
      kind: "pair",
      objectiveId: "O-1|extra",
      keyResultId: "KR-1|x",
    });
  });

  it("shows an honest empty state without fabricating outcomes", async () => {
    renderNavigator(emptyHierarchy());
    const trigger = screen.getByRole("button", { name: "Outcome navigator" });
    expect(trigger).toHaveAccessibleDescription("No outcomes");
    expect(screen.queryByRole("button", { name: /Select outcome/ })).not.toBeInTheDocument();
    await openPanel();
    expect(screen.getByRole("status")).toHaveTextContent("No outcomes are defined yet");
    expect(screen.queryByRole("button", { name: /Select outcome/ })).not.toBeInTheDocument();
  });

  it.each([
    { label: "wide", narrow: false },
    { label: "narrow", narrow: true },
  ])("opens a $label panel from the trigger, closes on Escape, and restores focus", async ({ narrow }) => {
    const restore = mockNarrow(narrow);
    try {
      renderNavigator(hierarchyOf(), { kind: "pair", objectiveId: "O-1", keyResultId: "KR-2" });
      const trigger = screen.getByRole("button", { name: "Outcome navigator" });
      expect(trigger).toHaveAccessibleDescription("O-1 · KR-2");
      expectStableDisclosure(trigger, false);
      expect(screen.queryByRole("button", { name: "Select outcome O-1 KR-1" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "All outcomes" })).not.toBeInTheDocument();

      await openPanel();
      expectStableDisclosure(trigger, true);
      expect(screen.getByRole("button", { name: "Select outcome O-1 KR-1" })).toBeInTheDocument();

      fireEvent.keyDown(trigger, { key: "Escape" });
      expectStableDisclosure(trigger, false);
      expect(trigger).toHaveFocus();
      expect(screen.queryByRole("button", { name: "Select outcome O-1 KR-1" })).not.toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("does not close or steal focus when Escape originates on an outside focused control", async () => {
    renderNavigator(hierarchyOf());
    const { trigger } = await openPanel();
    expectStableDisclosure(trigger, true);

    const outside = document.createElement("input");
    outside.setAttribute("aria-label", "Unrelated control");
    document.body.appendChild(outside);
    try {
      outside.focus();
      expect(outside).toHaveFocus();
      fireEvent.keyDown(outside, { key: "Escape" });
      fireEvent.keyDown(window, { key: "Escape" });
      expectStableDisclosure(trigger, true);
      expect(outside).toHaveFocus();
      expect(trigger).not.toHaveFocus();
      expect(screen.getByRole("button", { name: "Select outcome O-1 KR-1" })).toBeInTheDocument();
    } finally {
      outside.remove();
    }
  });

  it("does not close when a nested control already consumes Escape", async () => {
    renderNavigator(hierarchyOf());
    const { trigger } = await openPanel();
    const nested = screen.getByRole("button", { name: "All outcomes" });
    nested.addEventListener("keydown", (event) => {
      if (event.key === "Escape") event.preventDefault();
    });
    nested.focus();
    fireEvent.keyDown(nested, { key: "Escape" });
    expectStableDisclosure(trigger, true);
    expect(nested).toHaveFocus();
    expect(trigger).not.toHaveFocus();
    expect(screen.getByRole("button", { name: "Select outcome O-1 KR-1" })).toBeInTheDocument();
  });

  it("records KR progress from the supplied value only", async () => {
    renderNavigator(hierarchyOf());
    await openPanel();
    expect(
      within(screen.getByRole("button", { name: "Select outcome O-1 KR-1" }))
        .getByText("Recorded progress 40"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("button", { name: "Select outcome O-2 KR-1" }))
        .getByText("Unrecorded"),
    ).toBeInTheDocument();
  });

  it("keeps a scoped action name and exposes counts plus progress as a description", async () => {
    renderNavigator(hierarchyOf());
    await openPanel();
    const o1 = screen.getByRole("button", { name: "Select outcome O-1 KR-1" });
    const o2 = screen.getByRole("button", { name: "Select outcome O-2 KR-1" });
    expect(o1).toHaveAccessibleName("Select outcome O-1 KR-1");
    expect(o2).toHaveAccessibleName("Select outcome O-2 KR-1");
    expect(o1).toHaveAccessibleDescription("2 visible of 2 linked Recorded progress 40");
    expect(o2).toHaveAccessibleDescription("0 visible of 0 linked Unrecorded");
    expect(o1.getAttribute("aria-describedby")).toBeTruthy();
    expect(o2.getAttribute("aria-describedby")).not.toBe(o1.getAttribute("aria-describedby"));
  });

  it("summarizes All and Unassigned on the closed trigger without exposing hidden panel targets", () => {
    const { rerender, onFilterChange } = renderNavigator(hierarchyOf(), { kind: "all" });
    const trigger = screen.getByRole("button", { name: "Outcome navigator" });
    expect(trigger).toHaveAccessibleDescription("All outcomes");
    expectStableDisclosure(trigger, false);
    rerender(
      <OutcomeNavigator
        hierarchy={hierarchyOf()}
        filter={{ kind: "unassigned" }}
        onFilterChange={onFilterChange}
      />,
    );
    expect(trigger).toHaveAccessibleDescription("Unassigned outcome");
    expect(screen.queryByRole("button", { name: "Unassigned outcome" })).not.toBeInTheDocument();
  });
});
