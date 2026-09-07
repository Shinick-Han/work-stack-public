import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { projectOutcomeHierarchy } from "../../../domain/outcomeHierarchy";
import type { Objective, Task } from "../../../domain/types";
import { BoardOutcomeBreadcrumb } from "./BoardOutcomeBreadcrumb";
import type { WorkspaceTask } from "./types";

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

function domainTask(id: string, patch: Partial<Task> = {}): Task {
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

function section84() {
  const objectives: Objective[] = [
    objective("O-1", [
      { id: "KR-1", text: "O-1 KR-1", progress: 40 },
      { id: "KR-2", text: "O-1 KR-2" },
    ]),
    objective("O-2", [{ id: "KR-1", text: "O-2 KR-1" }]),
  ];
  const tasks: Task[] = [
    domainTask("T-1", {
      objective_ids: ["O-1"],
      key_result_refs: [{ objective_id: "O-1", key_result_id: "KR-1" }],
    }),
    domainTask("T-2", { objective_ids: ["O-1"] }),
    domainTask("T-3", {
      objective_ids: ["O-1"],
      key_result_refs: [
        { objective_id: "O-1", key_result_id: "KR-1" },
        { objective_id: "O-1", key_result_id: "KR-2" },
      ],
    }),
    domainTask("T-4"),
    domainTask("T-5", {
      objective_ids: ["O-1"],
      key_result_refs: [{ objective_id: "O-1", key_result_id: "KR-MISSING" }],
    }),
  ];
  const hierarchy = projectOutcomeHierarchy({
    workspaceId: WORKSPACE_ID,
    tasks,
    visibleTasks: tasks,
    objectives,
  });
  return { tasks: tasks as unknown as WorkspaceTask[], hierarchy };
}

describe("BoardOutcomeBreadcrumb", () => {
  it("renders resolved, multi-outcome, objective-only, floating, and unresolved trails", () => {
    const { tasks, hierarchy } = section84();
    const byId = Object.fromEntries(tasks.map((item) => [item.id, item]));
    const { rerender } = render(
      <BoardOutcomeBreadcrumb
        task={byId["T-1"]!}
        keyResultProjection={hierarchy.projection}
        onSelectObjective={vi.fn()}
        onSelectOutcome={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Focus objective O-1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Filter by outcome O-1 KR-1" })).toBeInTheDocument();

    rerender(
      <BoardOutcomeBreadcrumb
        task={byId["T-3"]!}
        keyResultProjection={hierarchy.projection}
        onSelectObjective={vi.fn()}
        onSelectOutcome={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Filter by outcome O-1 KR-1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Filter by outcome O-1 KR-2" })).toBeInTheDocument();

    rerender(
      <BoardOutcomeBreadcrumb
        task={byId["T-2"]!}
        keyResultProjection={hierarchy.projection}
        onSelectObjective={vi.fn()}
        onSelectOutcome={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Focus objective O-1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Filter by outcome/ })).not.toBeInTheDocument();

    rerender(
      <BoardOutcomeBreadcrumb
        task={byId["T-4"]!}
        keyResultProjection={hierarchy.projection}
        onSelectObjective={vi.fn()}
        onSelectOutcome={vi.fn()}
      />,
    );
    expect(screen.getByText("Unaligned")).toBeInTheDocument();

    rerender(
      <BoardOutcomeBreadcrumb
        task={byId["T-5"]!}
        keyResultProjection={hierarchy.projection}
        onSelectObjective={vi.fn()}
        onSelectOutcome={vi.fn()}
      />,
    );
    expect(screen.getByText("Unresolved outcome")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Filter by outcome O-1 KR-MISSING" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Filter by outcome O-2 KR-1" })).not.toBeInTheDocument();
  });

  it("stops mouse and keyboard activation from selecting the parent card", async () => {
    const user = userEvent.setup();
    const { tasks, hierarchy } = section84();
    const onSelectTask = vi.fn();
    const onSelectObjective = vi.fn();
    const onSelectOutcome = vi.fn();
    render(
      <article aria-label="T-1: T-1 title" onClick={() => onSelectTask("T-1")}>
        <BoardOutcomeBreadcrumb
          task={tasks[0]!}
          keyResultProjection={hierarchy.projection}
          onSelectObjective={onSelectObjective}
          onSelectOutcome={onSelectOutcome}
        />
      </article>,
    );

    await user.click(screen.getByRole("button", { name: "Focus objective O-1" }));
    expect(onSelectObjective).toHaveBeenCalledExactlyOnceWith("O-1");
    expect(onSelectTask).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Filter by outcome O-1 KR-1" }));
    expect(onSelectOutcome).toHaveBeenCalledExactlyOnceWith({
      objectiveId: "O-1",
      keyResultId: "KR-1",
    });
    expect(onSelectTask).not.toHaveBeenCalled();

    const objectiveButton = screen.getByRole("button", { name: "Focus objective O-1" });
    fireEvent.keyDown(objectiveButton, { key: "Enter" });
    expect(onSelectTask).not.toHaveBeenCalled();
    expect(onSelectObjective).toHaveBeenCalledTimes(1);
  });

  it("shows Objective and KR text without inert action buttons when no callbacks exist", () => {
    const { tasks, hierarchy } = section84();
    const byId = Object.fromEntries(tasks.map((item) => [item.id, item]));
    render(
      <BoardOutcomeBreadcrumb
        task={byId["T-1"]!}
        keyResultProjection={hierarchy.projection}
      />,
    );
    expect(screen.getByText("O-1")).toBeInTheDocument();
    expect(screen.getByText("KR-1")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("makes Objective and KR actions independently available", () => {
    const { tasks, hierarchy } = section84();
    const byId = Object.fromEntries(tasks.map((item) => [item.id, item]));
    const onSelectObjective = vi.fn();
    const onSelectOutcome = vi.fn();
    const { rerender } = render(
      <BoardOutcomeBreadcrumb
        task={byId["T-1"]!}
        keyResultProjection={hierarchy.projection}
        onSelectObjective={onSelectObjective}
      />,
    );
    expect(screen.getByRole("button", { name: "Focus objective O-1" })).toBeInTheDocument();
    expect(screen.getByText("KR-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Filter by outcome O-1 KR-1" })).not.toBeInTheDocument();

    rerender(
      <BoardOutcomeBreadcrumb
        task={byId["T-1"]!}
        keyResultProjection={hierarchy.projection}
        onSelectOutcome={onSelectOutcome}
      />,
    );
    expect(screen.getByRole("button", { name: "Filter by outcome O-1 KR-1" })).toBeInTheDocument();
    expect(screen.getByText("O-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Focus objective O-1" })).not.toBeInTheDocument();
  });
});
