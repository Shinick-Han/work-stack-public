import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { projectKeyResults } from "./keyResultModel";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";

import {
  BoardView,
  boardKeyboardCoordinates,
  boardStatusCollision,
  isBoardCardPointerBlocked,
} from "./BoardView";
import type { WorkspaceTask } from "./types";
import { projectOutcomeHierarchy } from "../../../domain/outcomeHierarchy";
import type { Objective, Task } from "../../../domain/types";

if (typeof globalThis.PointerEvent === "undefined") {
  class PointerEventPolyfill extends MouseEvent {
    pointerId: number;
    width: number;
    height: number;
    pressure: number;
    tangentialPressure: number;
    tiltX: number;
    tiltY: number;
    twist: number;
    pointerType: string;
    isPrimary: boolean;
    constructor(type: string, params: MouseEventInit & {
      pointerId?: number;
      pointerType?: string;
      isPrimary?: boolean;
    } = {}) {
      super(type, params);
      this.pointerId = params.pointerId ?? 1;
      this.width = 1;
      this.height = 1;
      this.pressure = 0.5;
      this.tangentialPressure = 0;
      this.tiltX = 0;
      this.tiltY = 0;
      this.twist = 0;
      this.pointerType = params.pointerType ?? "mouse";
      this.isPrimary = params.isPrimary ?? true;
    }
  }
  globalThis.PointerEvent = PointerEventPolyfill as unknown as typeof PointerEvent;
}

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


function outcomeTask(id: string, extra: Record<string, unknown> = {}) {
  return { id, title: "Task " + id, status: "open", priority: "P2", due: null, tags: [], objective_ids: [], dependencies: [], subtasks: [], context_count: 0, revision: 1, ...extra } as never;
}

const outcomeProjection = projectKeyResults({
  workspaceId: "W1",
  tasks: [
    outcomeTask("T-1", { uid: "u1", notes: [], objective_ids: ["O-A"], key_result_refs: [{ objective_id: "O-A", key_result_id: "KR-1" }] }),
    outcomeTask("T-2", { uid: "u2", notes: [], objective_ids: ["O-A"], key_result_refs: [{ objective_id: "O-A", key_result_id: "KR-9" }] }),
    outcomeTask("T-3", { uid: "u3", notes: [] }),
  ],
  objectives: [
    { id: "O-A", objective: "Objective A", revision: 1, key_results: [{ id: "KR-1", text: "A outcome" }] } as never,
  ],
});

describe("outcome breadcrumbs", () => {
  it("renders one card per Task with resolved, unresolved and unaligned labels", () => {
    render(
      <BoardView
        tasks={[task("T-1"), task("T-2"), task("T-3")]}
        keyResultProjection={outcomeProjection}
        onChangeTaskStatus={vi.fn()}
        onSelectTask={vi.fn()}
      />,
    );

    expect(screen.getAllByRole("article")).toHaveLength(3);
    const t1 = card("T-1");
    expect(within(t1).getByText("O-A")).toBeInTheDocument();
    expect(within(t1).getByText("KR-1")).toBeInTheDocument();
    expect(within(t1).queryByRole("button", { name: "Filter by outcome O-A KR-1" })).not.toBeInTheDocument();
    expect(screen.getByText("Unresolved outcome")).toBeInTheDocument();
    expect(screen.getByText("Unaligned")).toBeInTheDocument();
  });

  it("chip activation chooses the pair without selecting the card", async () => {
    const onSelectOutcome = vi.fn();
    const onSelectTask = vi.fn();
    render(
      <BoardView
        tasks={[task("T-1")]}
        keyResultProjection={outcomeProjection}
        onSelectOutcome={onSelectOutcome}
        onChangeTaskStatus={vi.fn()}
        onSelectTask={onSelectTask}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Filter by outcome O-A KR-1" }));

    expect(onSelectOutcome).toHaveBeenCalledExactlyOnceWith({ objectiveId: "O-A", keyResultId: "KR-1" });
    expect(onSelectTask).not.toHaveBeenCalled();
  });
});

describe("BoardView §8.4 breadcrumbs", () => {
  function oracleTask(id: string, patch: Partial<Task> = {}): Task {
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

  const objectives: Objective[] = [
    {
      id: "O-1",
      objective: "O-1 objective",
      revision: 1,
      key_results: [
        { id: "KR-1", text: "O-1 KR-1", progress: 40 },
        { id: "KR-2", text: "O-1 KR-2" },
      ],
    },
    {
      id: "O-2",
      objective: "O-2 objective",
      revision: 1,
      key_results: [{ id: "KR-1", text: "O-2 KR-1" }],
    },
  ];
  const oracleTasks: Task[] = [
    oracleTask("T-1", {
      objective_ids: ["O-1"],
      key_result_refs: [{ objective_id: "O-1", key_result_id: "KR-1" }],
    }),
    oracleTask("T-2", { objective_ids: ["O-1"] }),
    oracleTask("T-3", {
      objective_ids: ["O-1"],
      key_result_refs: [
        { objective_id: "O-1", key_result_id: "KR-1" },
        { objective_id: "O-1", key_result_id: "KR-2" },
      ],
    }),
    oracleTask("T-4"),
    oracleTask("T-5", {
      objective_ids: ["O-1"],
      key_result_refs: [{ objective_id: "O-1", key_result_id: "KR-MISSING" }],
    }),
  ];
  const hierarchy = projectOutcomeHierarchy({
    workspaceId: "W-oracle",
    tasks: oracleTasks,
    visibleTasks: oracleTasks,
    objectives,
  });

  it("keeps one card per supplied Task and never fabricates a zero-linked KR card", () => {
    render(
      <BoardView
        tasks={oracleTasks as unknown as WorkspaceTask[]}
        keyResultProjection={hierarchy.projection}
        onSelectTask={vi.fn()}
        onSelectObjective={vi.fn()}
        onSelectOutcome={vi.fn()}
        onChangeTaskStatus={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getAllByRole("article")).toHaveLength(5);
    expect(screen.getByRole("article", { name: "T-3: T-3 title" })).toBeInTheDocument();
    expect(within(screen.getByRole("article", { name: "T-3: T-3 title" }))
      .getByRole("button", { name: "Filter by outcome O-1 KR-2" })).toBeInTheDocument();
    expect(within(screen.getByRole("article", { name: "T-2: T-2 title" }))
      .queryByRole("button", { name: /Filter by outcome/ })).not.toBeInTheDocument();
    expect(within(screen.getByRole("article", { name: "T-4: T-4 title" }))
      .getByText("Unaligned")).toBeInTheDocument();
    expect(within(screen.getByRole("article", { name: "T-5: T-5 title" }))
      .getByText("Unresolved outcome")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Filter by outcome O-2 KR-1" })).not.toBeInTheDocument();
  });
});

describe("BoardView track containment", () => {
  it("nests cards inside column tracks and binds containment CSS to those nodes", () => {
    const longTitle = task("T-0001", {
      title: "Supercalifragilisticexpialidocious-unbroken-title-that-must-stay-in-track",
      detail: "Detail-without-spaces-".repeat(12),
      dependencies: ["T-GONE"],
    });
    const { container } = renderBoard([longTitle], vi.fn().mockResolvedValue(undefined));

    const track = container.querySelector('[data-containment="board-track"]');
    const columns = container.querySelectorAll('[data-containment="board-column"]');
    const cardNode = screen.getByRole("article", {
      name: `T-0001: ${longTitle.title}`,
    });

    expect(track).toHaveClass("wsv-board");
    expect(columns).toHaveLength(4);
    expect(cardNode).toHaveClass("wsv-task-card");
    expect(cardNode).toHaveAttribute("data-containment", "board-card");
    expect(cardNode.closest('[data-containment="board-column"]')).not.toBeNull();
    expect(track?.contains(cardNode)).toBe(true);

    const css = readFileSync("src/features/workspace/views/workspace-views.css", "utf-8");
    const bounded = css.slice(css.indexOf("T-0045"));
    expect(bounded).toContain(".wsv-board-column");
    expect(bounded).toContain(".wsv-task-card");
    expect(bounded).toMatch(/minmax\(0,\s*1fr\)/);
    expect(bounded).toMatch(/overflow:\s*hidden/);
    expect(bounded).toMatch(/overflow-wrap:\s*anywhere/);
    expect(bounded).toMatch(/inset 0 0 0 2px var\(--wsv-card-focus-ring\)/);
  });
});

const BOARD_STATUSES = ["open", "started", "done", "dropped"] as const;
const COL_H = 480;

function clientRect(x: number, y: number, width: number, height: number): DOMRect {
  return {
    x,
    y,
    width,
    height,
    top: y,
    left: x,
    right: x + width,
    bottom: y + height,
    toJSON() {
      return this;
    },
  } as DOMRect;
}

function pointer(clientX: number, clientY: number, extra: Record<string, unknown> = {}) {
  return {
    isPrimary: true,
    button: 0,
    buttons: 1,
    pointerId: 1,
    pointerType: "mouse" as const,
    clientX,
    clientY,
    ...extra,
  };
}

function columnIndex(status: string) {
  return Math.max(0, BOARD_STATUSES.indexOf(status as (typeof BOARD_STATUSES)[number]));
}

function stubBoardGeometry(
  columnWidth: number,
  heights: Partial<Record<(typeof BOARD_STATUSES)[number], number>> = {},
) {
  HTMLElement.prototype.scrollIntoView = () => undefined;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const column = this.closest("[data-containment='board-column']");
    const status =
      (column?.querySelector("[id^='wsv-column-']")?.id.replace("wsv-column-", "") ??
        "open") as (typeof BOARD_STATUSES)[number];
    const left = columnIndex(status) * columnWidth;
    const height = heights[status] ?? COL_H;
    if (this.getAttribute("data-containment") === "board-column") {
      return clientRect(left, 0, columnWidth, height);
    }
    if (this.getAttribute("data-containment") === "board-card") {
      return clientRect(left + 16, 48, Math.max(48, columnWidth - 32), 160);
    }
    return clientRect(left, 0, columnWidth, height);
  });
}

function unequalColumnGeometry(columnWidth = 280) {
  const heights = { open: 920, started: 72, done: 880, dropped: 360 } as const;
  const droppableRects = new Map(
    BOARD_STATUSES.map((status, index) => {
      const height = heights[status];
      const left = index * columnWidth;
      return [
        status,
        {
          left,
          top: 0,
          width: columnWidth,
          height,
          right: left + columnWidth,
          bottom: height,
        },
      ];
    }),
  );
  const droppableContainers = BOARD_STATUSES.map((status) => ({
    id: status,
    disabled: false,
    data: { current: { type: "column" } },
    rect: { current: droppableRects.get(status) },
  }));
  return { columnWidth, droppableRects, droppableContainers, heights };
}

async function dragToStatus(
  source: HTMLElement,
  status: (typeof BOARD_STATUSES)[number],
  columnWidth: number,
  extra: Record<string, unknown> = {},
) {
  const origin = source.getBoundingClientRect();
  const fromX = origin.left + 24;
  const fromY = origin.top + 12;
  const toX = columnIndex(status) * columnWidth + columnWidth / 2;
  const toY = 120;
  await act(async () => {
    fireEvent.pointerDown(source, pointer(fromX, fromY, extra));
  });
  await act(async () => {
    fireEvent.pointerMove(document, pointer(fromX + 20, fromY, extra));
  });
  await act(async () => {
    fireEvent.pointerMove(document, pointer(toX, toY, extra));
  });
  await act(async () => {
    fireEvent.pointerMove(document, pointer(toX + 1, toY, extra));
  });
  await act(async () => {
    fireEvent.pointerUp(document, pointer(toX, toY, extra));
  });
}

function keyboardArgs(columnWidth: number, over: (typeof BOARD_STATUSES)[number] = "open") {
  const droppableRects = new Map(
    BOARD_STATUSES.map((status, index) => [
      status,
      {
        left: index * columnWidth,
        top: 0,
        width: columnWidth,
        height: COL_H,
        right: (index + 1) * columnWidth,
        bottom: COL_H,
      },
    ]),
  );
  const cardWidth = Math.max(40, columnWidth - 32);
  const overIndex = columnIndex(over);
  const collisionRect = {
    left: overIndex * columnWidth + 12,
    top: 40,
    width: cardWidth,
    height: 120,
    right: overIndex * columnWidth + 12 + cardWidth,
    bottom: 160,
  };
  return {
    active: "T-0001",
    currentCoordinates: { x: collisionRect.left, y: collisionRect.top },
    context: { droppableRects, collisionRect, over: { id: over } },
  };
}

async function pickupHandle(handle: HTMLElement) {
  handle.focus();
  await act(async () => {
    fireEvent.keyDown(handle, { key: " ", code: "Space" });
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("BoardView full-card pointer drag", () => {
  const wide = 320;
  const narrow = 120;

  beforeEach(() => {
    stubBoardGeometry(wide);
  });

  it("starts a drag from multiple safe card-body points and drops into every valid status column", async () => {
    const cases: Array<{
      origin: (node: HTMLElement) => HTMLElement;
      fromStatus: (typeof BOARD_STATUSES)[number];
      toStatus: (typeof BOARD_STATUSES)[number];
    }> = [
      {
        origin: (node) => within(node).getByText("P2"),
        fromStatus: "open",
        toStatus: "started",
      },
      {
        origin: (node) => within(node).getByText("T-0001"),
        fromStatus: "open",
        toStatus: "done",
      },
      {
        origin: (node) => within(node).getByLabelText("Due for T-0001: No due date"),
        fromStatus: "open",
        toStatus: "dropped",
      },
      {
        origin: (node) => node,
        fromStatus: "started",
        toStatus: "open",
      },
    ];

    for (const scenario of cases) {
      const onChange = vi.fn().mockResolvedValue(undefined);
      const { unmount } = renderBoard(
        [task("T-0001", { status: scenario.fromStatus })],
        onChange,
      );
      const source = scenario.origin(card("T-0001"));
      await dragToStatus(source, scenario.toStatus, wide);
      await waitFor(() => expect(onChange).toHaveBeenCalledWith("T-0001", scenario.toStatus));
      expect(onChange).toHaveBeenCalledOnce();
      unmount();
    }
  });

  it("does not start a drag from nested selects, buttons, or links, while those controls still work", async () => {
    const wrap = document.createElement("div");
    wrap.innerHTML = [
      '<a href="/x">link</a>',
      "<select></select>",
      "<button>go</button>",
      "<input />",
      "<h3>Title</h3>",
      "<p>Detail</p>",
      '<span><button class="wsv-drag-handle">handle</button></span>',
    ].join("");
    const [link, selectEl, button, input, heading, detail, handleWrap] = Array.from(wrap.children);
    const handle = handleWrap.querySelector("button");
    const nested = document.createElement("span");
    button.append(nested);
    expect(isBoardCardPointerBlocked(link)).toBe(true);
    expect(isBoardCardPointerBlocked(selectEl)).toBe(true);
    expect(isBoardCardPointerBlocked(button)).toBe(true);
    expect(isBoardCardPointerBlocked(nested)).toBe(true);
    expect(isBoardCardPointerBlocked(input)).toBe(true);
    expect(isBoardCardPointerBlocked(heading)).toBe(true);
    expect(isBoardCardPointerBlocked(detail)).toBe(true);
    expect(isBoardCardPointerBlocked(handle)).toBe(false);

    const onChange = vi.fn().mockResolvedValue(undefined);
    const onSelectTask = vi.fn();
    const onSelectObjective = vi.fn();
    const blocked = task("T-0101", { dependencies: ["T-0100"], objective_ids: ["O-9"] });
    const prerequisite = task("T-0100", { title: "Approve launch", status: "started" });
    render(
      <BoardView
        tasks={[blocked]}
        referenceTasks={[blocked, prerequisite]}
        selectedTaskId={null}
        onSelectTask={onSelectTask}
        onSelectObjective={onSelectObjective}
        onChangeTaskStatus={onChange}
      />,
    );

    await dragToStatus(statusSelect("T-0101"), "started", wide);
    await dragToStatus(
      within(card("T-0101")).getByRole("button", { name: "Blocked by T-0100: Approve launch" }),
      "done",
      wide,
    );
    await dragToStatus(
      within(card("T-0101")).getByRole("button", { name: "Focus objective O-9" }),
      "dropped",
      wide,
    );
    expect(onChange).not.toHaveBeenCalled();

    await userEvent.click(
      within(card("T-0101")).getByRole("button", { name: "Blocked by T-0100: Approve launch" }),
    );
    expect(onSelectTask).toHaveBeenCalledWith("T-0100");
    await userEvent.click(
      within(card("T-0101")).getByRole("button", { name: "Focus objective O-9" }),
    );
    expect(onSelectObjective).toHaveBeenCalledWith("O-9");
    await userEvent.selectOptions(statusSelect("T-0101"), "started");
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith("T-0101", "started");
  });

  it("leaves title and detail to real text selection instead of starting a card drag", async () => {
    const onChange = vi.fn().mockResolvedValue(undefined);
    renderBoard([task("T-0001")], onChange);
    const title = within(card("T-0001")).getByRole("heading");
    const detail = within(card("T-0001")).getByText("Board status test");
    await dragToStatus(title, "started", wide);
    await dragToStatus(detail, "done", wide);
    expect(onChange).not.toHaveBeenCalled();

    const range = document.createRange();
    range.selectNodeContents(title);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    expect(selection?.toString()).toContain("Task T-0001");
    expect(document.querySelector(".wsv-task-card.is-dragging")).toBeNull();
  });

  it("ignores non-primary pointers so they cannot initiate a drag", async () => {
    const onChange = vi.fn().mockResolvedValue(undefined);
    renderBoard([task("T-0001")], onChange);
    await dragToStatus(within(card("T-0001")).getByText("P2"), "started", wide, {
      isPrimary: false,
      pointerId: 2,
    });
    expect(onChange).not.toHaveBeenCalled();
    expect(document.querySelector(".wsv-task-card.is-dragging")).toBeNull();
  });

  it("selects the card on a click that never exceeds the drag distance", async () => {
    const onSelectTask = vi.fn();
    const onChange = vi.fn().mockResolvedValue(undefined);
    render(
      <BoardView
        tasks={[task("T-0001")]}
        selectedTaskId={null}
        onSelectTask={onSelectTask}
        onChangeTaskStatus={onChange}
      />,
    );
    const chrome = within(card("T-0001")).getByText("P2");
    const origin = chrome.getBoundingClientRect();
    fireEvent.pointerDown(chrome, pointer(origin.left + 8, origin.top + 8));
    fireEvent.pointerMove(document, pointer(origin.left + 11, origin.top + 8));
    fireEvent.pointerUp(chrome, pointer(origin.left + 11, origin.top + 8));
    fireEvent.click(chrome);
    expect(onSelectTask).toHaveBeenCalledWith("T-0001");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("moves exactly one semantic column per arrow at narrow and normal widths", () => {
    for (const columnWidth of [narrow, wide]) {
      const fromOpen = keyboardArgs(columnWidth, "open");
      const right = boardKeyboardCoordinates(
        { code: "ArrowRight" } as KeyboardEvent,
        fromOpen as never,
      );
      expect(right?.x).toBeCloseTo(columnWidth + columnWidth / 2 - (columnWidth - 32) / 2);
      expect(right?.x).toBeGreaterThanOrEqual(columnWidth);
      expect(right?.x).toBeLessThan(columnWidth * 2);

      const fromStarted = keyboardArgs(columnWidth, "started");
      const left = boardKeyboardCoordinates(
        { code: "ArrowLeft" } as KeyboardEvent,
        fromStarted as never,
      );
      expect(left?.x).toBeCloseTo(columnWidth / 2 - (columnWidth - 32) / 2);
      expect(left?.x).toBeLessThan(columnWidth);
    }
  });

  it("ignores a stale over id so ArrowRight still targets the adjacent status", () => {
    const columnWidth = wide;
    const fromOpen = keyboardArgs(columnWidth, "open");
    fromOpen.context.over = { id: "done" };
    const right = boardKeyboardCoordinates(
      { code: "ArrowRight" } as KeyboardEvent,
      fromOpen as never,
    );
    expect(right?.x).toBeGreaterThanOrEqual(columnWidth);
    expect(right?.x).toBeLessThan(columnWidth * 2);
  });

  it("targets the horizontally containing status even when neighbor columns are taller", () => {
    const { columnWidth, droppableRects, droppableContainers } = unequalColumnGeometry(wide);
    const pointerX = columnWidth + 48;
    const collisions = boardStatusCollision({
      active: { id: "T-0001" },
      collisionRect: {
        left: columnWidth - 40,
        top: 16,
        width: 220,
        height: 140,
        right: columnWidth + 180,
        bottom: 156,
      },
      droppableRects,
      droppableContainers,
      pointerCoordinates: { x: pointerX, y: 24 },
    } as never);
    expect(collisions).toHaveLength(1);
    expect(collisions[0]?.id).toBe("started");
  });

  it("falls back to the nearest horizontal column center when the pointer is in a gap", () => {
    const droppableRects = new Map([
      ["open", { left: 0, top: 0, width: 200, height: 920, right: 200, bottom: 920 }],
      ["started", { left: 280, top: 0, width: 200, height: 72, right: 480, bottom: 72 }],
      ["done", { left: 560, top: 0, width: 200, height: 880, right: 760, bottom: 880 }],
      ["dropped", { left: 840, top: 0, width: 200, height: 360, right: 1040, bottom: 360 }],
    ]);
    const droppableContainers = BOARD_STATUSES.map((status) => ({
      id: status,
      disabled: false,
      data: { current: { type: "column" } },
      rect: { current: droppableRects.get(status) },
    }));
    const collisions = boardStatusCollision({
      active: { id: "T-0001" },
      collisionRect: {
        left: 230,
        top: 8,
        width: 40,
        height: 40,
        right: 270,
        bottom: 48,
      },
      droppableRects,
      droppableContainers,
      pointerCoordinates: null,
    } as never);
    expect(collisions[0]?.id).toBe("started");
  });

  it("drops into In progress from a pointer inside that column despite unequal heights", async () => {
    stubBoardGeometry(wide, { open: 920, started: 72, done: 880, dropped: 360 });
    const onChange = vi.fn().mockResolvedValue(undefined);
    renderBoard(
      [
        task("T-0001"),
        ...Array.from({ length: 6 }, (_, index) => task(`T-open-${index}`)),
        task("T-done-1", { status: "done" }),
        task("T-done-2", { status: "done" }),
        task("T-done-3", { status: "done" }),
      ],
      onChange,
    );
    await dragToStatus(within(card("T-0001")).getByText("P2"), "started", wide);
    await waitFor(() => expect(onChange).toHaveBeenCalledOnce());
    expect(onChange).toHaveBeenCalledWith("T-0001", "started");
  });

  it("moves a card with the keyboard handle, announces status, and cancels with Escape", async () => {
    const onChange = vi.fn().mockResolvedValue(undefined);
    renderBoard([task("T-0001")], onChange);
    const handle = dragHandle("T-0001");
    await pickupHandle(handle);
    await waitFor(() => {
      expect(document.querySelector(".wsv-task-card.is-dragging:not(.is-overlay)")).not.toBeNull();
      expect(document.querySelector("[id^='DndLiveRegion']")).toHaveTextContent(
        /Picked up T-0001: Task T-0001|T-0001: Task T-0001 is over Open/,
      );
    });

    await act(async () => {
      fireEvent.keyDown(document, { key: "Escape", code: "Escape" });
    });
    await waitFor(() => {
      expect(document.querySelector("[id^='DndLiveRegion']")).toHaveTextContent(
        /Move cancelled\. T-0001: Task T-0001 returned to its column\./,
      );
    });
    expect(onChange).not.toHaveBeenCalled();

    await pickupHandle(handle);
    await act(async () => {
      fireEvent.keyDown(document, { key: "ArrowRight", code: "ArrowRight" });
    });
    await act(async () => {
      fireEvent.keyDown(document, { key: " ", code: "Space" });
    });
    await waitFor(() => expect(onChange).toHaveBeenCalledWith("T-0001", "started"));
    await waitFor(() => {
      expect(document.querySelector("[id^='DndLiveRegion']")).toHaveTextContent(
        /Moved T-0001: Task T-0001 to In progress\./,
      );
    });
  });

  it("drops into the adjacent column after one ArrowRight at a narrow column width", async () => {
    stubBoardGeometry(narrow);
    const onChange = vi.fn().mockResolvedValue(undefined);
    renderBoard([task("T-0001")], onChange);
    await pickupHandle(dragHandle("T-0001"));
    await act(async () => {
      fireEvent.keyDown(document, { key: "ArrowRight", code: "ArrowRight" });
    });
    await act(async () => {
      fireEvent.keyDown(document, { key: " ", code: "Space" });
    });
    await waitFor(() => expect(onChange).toHaveBeenCalledOnce());
    expect(onChange).toHaveBeenCalledWith("T-0001", "started");
  });

  it("keeps grab/grabbing affordance and selectable card text without touching containment tokens", () => {
    renderBoard([task("T-0001")], vi.fn().mockResolvedValue(undefined));
    expect(card("T-0001")).toHaveClass("is-grabbable");
    const css = readFileSync("src/features/workspace/views/workspace-views.css", "utf-8");
    const section = css.slice(css.indexOf("T-0046"));
    expect(section).toMatch(/cursor:\s*grab/);
    expect(section).toMatch(/cursor:\s*grabbing/);
    expect(section).toMatch(/cursor:\s*text/);
    expect(section).toMatch(/user-select:\s*text/);
    const bounded = css.slice(css.indexOf("T-0045"));
    expect(bounded).toMatch(/overflow:\s*hidden/);
    expect(bounded).toMatch(/overflow-wrap:\s*anywhere/);
  });
});
