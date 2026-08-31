import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BoardView } from "./BoardView";
import type { WorkspaceTask } from "./types";

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
  reject: (reason: Error) => void;
}

function deferred(): Deferred {
  let resolve!: () => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function task(id: string, changes: Partial<WorkspaceTask> = {}): WorkspaceTask {
  return {
    id,
    title: `Task ${id}`,
    detail: "Board status test",
    status: "open",
    priority: "P2",
    due: null,
    objective_ids: [],
    dependencies: [],
    tags: [],
    context_count: 0,
    revision: 1,
    ...changes,
  };
}

function renderBoard(
  tasks: WorkspaceTask[],
  onChangeTaskStatus: (taskId: string, status: "open" | "started" | "done" | "dropped") => Promise<void>,
) {
  return render(
    <BoardView
      tasks={tasks}
      selectedTaskId={null}
      onSelectTask={vi.fn()}
      onChangeTaskStatus={onChangeTaskStatus}
    />,
  );
}

function card(taskId: string) {
  return screen.getByRole("article", { name: `${taskId}: Task ${taskId}` });
}

function statusSelect(taskId: string) {
  return within(card(taskId)).getByRole("combobox", { name: `Change ${taskId} status` });
}

function dragHandle(taskId: string) {
  return within(card(taskId)).getByRole("button", { name: `Move Task ${taskId}` });
}

describe("BoardView status changes", () => {
  it("keeps only the target card pending until deferred success settles", async () => {
    const request = deferred();
    const onChange = vi.fn(() => request.promise);
    renderBoard([task("T-0001"), task("T-0002")], onChange);

    await userEvent.selectOptions(statusSelect("T-0001"), "started");

    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith("T-0001", "started");
    expect(statusSelect("T-0001")).toBeDisabled();
    expect(dragHandle("T-0001")).toBeDisabled();
    expect(within(card("T-0001")).getByText("Saving…")).toBeInTheDocument();
    expect(statusSelect("T-0002")).toBeEnabled();
    expect(dragHandle("T-0002")).toBeEnabled();
    expect(within(card("T-0002")).getByText("Move to")).toBeInTheDocument();

    await act(async () => request.resolve());

    expect(statusSelect("T-0001")).toBeEnabled();
    expect(dragHandle("T-0001")).toBeEnabled();
    expect(statusSelect("T-0001")).toHaveValue("started");
    expect(within(card("T-0001")).getByText("Move to")).toBeInTheDocument();
  });

  it("uses a synchronous task lock to collapse same-tick duplicate events", async () => {
    const request = deferred();
    const onChange = vi.fn(() => request.promise);
    renderBoard([task("T-0001")], onChange);
    const select = statusSelect("T-0001") as HTMLSelectElement;

    act(() => {
      select.value = "started";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      select.value = "done";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });

    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith("T-0001", "started");
    await act(async () => request.resolve());
  });

  it("restores only the failed card and surfaces the rejection", async () => {
    const request = deferred();
    renderBoard([task("T-0001"), task("T-0002")], () => request.promise);

    await userEvent.selectOptions(statusSelect("T-0001"), "started");
    await act(async () => request.reject(new Error("Revision conflict")));

    expect(statusSelect("T-0001")).toHaveValue("open");
    expect(statusSelect("T-0001")).toBeEnabled();
    expect(statusSelect("T-0002")).toHaveValue("open");
    expect(screen.getByRole("alert")).toHaveTextContent("Revision conflict");
  });

  it("reveals a refreshed authoritative status after a rejected optimistic move", async () => {
    const request = deferred();
    const onChange = vi.fn(() => request.promise);
    const original = task("T-0001");
    const view = renderBoard([original], onChange);

    await userEvent.selectOptions(statusSelect("T-0001"), "done");
    view.rerender(
      <BoardView
        tasks={[{ ...original, status: "started", revision: 2 }]}
        selectedTaskId={null}
        onSelectTask={vi.fn()}
        onChangeTaskStatus={onChange}
      />,
    );

    await act(async () => request.reject(new Error("Revision conflict")));

    expect(statusSelect("T-0001")).toHaveValue("started");
    expect(screen.getByRole("alert")).toHaveTextContent("Revision conflict");
  });

  it("preserves task B success when task A fails concurrently", async () => {
    const first = deferred();
    const second = deferred();
    const onChange = vi.fn((taskId: string) => (
      taskId === "T-0001" ? first.promise : second.promise
    ));
    renderBoard([task("T-0001"), task("T-0002")], onChange);

    await userEvent.selectOptions(statusSelect("T-0001"), "started");
    await userEvent.selectOptions(statusSelect("T-0002"), "done");
    expect(statusSelect("T-0001")).toBeDisabled();
    expect(statusSelect("T-0002")).toBeDisabled();

    await act(async () => second.resolve());
    expect(statusSelect("T-0002")).toHaveValue("done");
    expect(statusSelect("T-0002")).toBeEnabled();
    expect(statusSelect("T-0001")).toBeDisabled();

    await act(async () => first.reject(new Error("Task A failed")));
    expect(statusSelect("T-0001")).toHaveValue("open");
    expect(statusSelect("T-0002")).toHaveValue("done");
    expect(screen.getByRole("alert")).toHaveTextContent("Task A failed");
  });
});

describe("BoardView dependency readiness", () => {
  it("shows an advisory blocker and opens the prerequisite without selecting the blocked task", async () => {
    const onSelectTask = vi.fn();
    const prerequisite = task("T-0100", { title: "Approve launch", status: "started" });
    const blocked = task("T-0101", { dependencies: [prerequisite.id] });

    render(
      <BoardView
        tasks={[blocked]}
        referenceTasks={[blocked, prerequisite]}
        selectedTaskId={null}
        onSelectTask={onSelectTask}
        onChangeTaskStatus={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    const readiness = within(card(blocked.id)).getByRole("button", {
      name: "Blocked by T-0100: Approve launch",
    });
    expect(readiness).toBeVisible();
    await userEvent.click(readiness);
    expect(onSelectTask).toHaveBeenCalledOnce();
    expect(onSelectTask).toHaveBeenCalledWith(prerequisite.id);
  });

  it("uses the full reference set so a completed dependency outside the filtered cards is satisfied", () => {
    const prerequisite = task("T-0100", { status: "done" });
    const ready = task("T-0101", { dependencies: [prerequisite.id] });

    render(
      <BoardView
        tasks={[ready]}
        referenceTasks={[ready, prerequisite]}
        selectedTaskId={null}
        onSelectTask={vi.fn()}
        onChangeTaskStatus={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(within(card(ready.id)).queryByText(/Blocked by/)).not.toBeInTheDocument();
  });
});

describe("BoardView subtask progress", () => {
  it("shows completed and total subtasks without opening the Task", () => {
    const withSteps = task("T-0300", {
      subtasks: [
        { id: "S-1", title: "Draft", status: "done" },
        { id: "S-2", title: "Review", status: "open" },
      ],
    });

    renderBoard([withSteps], vi.fn().mockResolvedValue(undefined));

    expect(within(card(withSteps.id)).getByLabelText("Steps for T-0300: 1 of 2 done"))
      .toBeVisible();
  });
});

describe("BoardView objective alignment", () => {
  it("focuses an aligned Objective without selecting the Task card", async () => {
    const onSelectObjective = vi.fn();
    const onSelectTask = vi.fn();
    const aligned = task("T-0400", { objective_ids: ["O-9"] });

    render(
      <BoardView
        tasks={[aligned]}
        selectedTaskId={null}
        onSelectTask={onSelectTask}
        onSelectObjective={onSelectObjective}
        onChangeTaskStatus={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    await userEvent.click(within(card(aligned.id)).getByRole("button", { name: "Focus objective O-9" }));
    expect(onSelectObjective).toHaveBeenCalledOnce();
    expect(onSelectObjective).toHaveBeenCalledWith("O-9");
    expect(onSelectTask).not.toHaveBeenCalled();

    const objectiveButton = within(card(aligned.id)).getByRole("button", { name: "Focus objective O-9" });
    fireEvent.keyDown(objectiveButton, { key: "Enter" });
    expect(onSelectTask).not.toHaveBeenCalled();
  });
});

describe("BoardView due timing", () => {
  it("shows an active overdue date as a local-calendar urgency label", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 30, 12, 0, 0));
    try {
      const overdue = task("T-0500", { due: "2026-08-29", status: "open" });
      renderBoard([overdue], vi.fn().mockResolvedValue(undefined));

      expect(within(card(overdue.id)).getByLabelText("Due for T-0500: 1 day overdue"))
        .toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });
});
